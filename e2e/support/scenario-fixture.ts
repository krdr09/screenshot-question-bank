import { createHash, randomBytes } from "node:crypto";
import { spawn } from "node:child_process";
import { constants } from "node:fs";
import { copyFile, mkdir, readFile, readdir, realpath, rmdir, stat } from "node:fs/promises";
import path from "node:path";

import {
  test as base,
  type Browser,
  type BrowserContext,
  type Page,
  type Playwright,
} from "@playwright/test";

import { launchBrowser } from "./browser";
import { attachPageDiagnostics, type PageDiagnostics } from "./diagnostics";
import {
  buildChildEnvironment,
  createScenarioEnvironment,
  removeScenarioDirectory,
  type ScenarioName,
  type ScenarioEnvironment,
} from "./environment";
import { pythonExecutable } from "./server";
import {
  recordedIdentityExists,
  snapshotDescendantProcesses,
  startServer,
  stopServer,
  terminateRecordedIdentity,
  type ServerHandle,
  type StopReport,
  type TrackedProcessIdentity,
} from "./server";
import { collectFailureEvidence } from "./evidence";
import { ResourceCleanupError } from "./lifecycle-error";

export interface ScenarioData {
  scenario: string;
  root: string;
  importDirectories: string[];
  images: string[];
  manifest: string;
  expectations: Record<string, unknown>;
}

export function createRunId(now = new Date(), random = randomBytes(4).toString("hex")): string {
  const timestamp = now.toISOString().replace(/[-:.]/g, "");
  if (!/^[a-f0-9]{8}$/i.test(random)) throw new Error("run-id 随机段格式无效");
  return `${timestamp}-${random.toLocaleLowerCase()}`;
}

export async function prepareScenarioData(environment: ScenarioEnvironment): Promise<ScenarioData> {
  const python = pythonExecutable(environment);
  const backendRoot = path.join(environment.repoRoot, "backend");
  const fixtureRoot = path.dirname(environment.mockOcrManifestPath);
  const result = await new Promise<{ code: number; stdout: string; stderr: string }>(
    (resolve, reject) => {
      const child = spawn(
        python,
        [
          "-m",
          "app.testing.e2e_scenarios",
          "--scenario",
          environment.scenarioName,
          "--output",
          fixtureRoot,
        ],
        {
          cwd: backendRoot,
          env: buildChildEnvironment(environment),
          windowsHide: true,
          stdio: ["ignore", "pipe", "pipe"],
        },
      );
      let stdout = "";
      let stderr = "";
      child.stdout.setEncoding("utf8");
      child.stderr.setEncoding("utf8");
      child.stdout.on("data", (chunk) => (stdout += chunk));
      child.stderr.on("data", (chunk) => (stderr += chunk));
      child.once("error", reject);
      child.once("close", (code) => resolve({ code: code ?? -1, stdout, stderr }));
    },
  );
  if (result.code !== 0) {
    throw new Error(`合成场景生成失败：${result.stderr || result.stdout}`);
  }
  const summary = JSON.parse(result.stdout) as {
    scenario: string;
    root: string;
    import_directories: string[];
    images: string[];
    manifest: string;
    expectations: Record<string, unknown>;
  };
  const root = path.resolve(summary.root);
  if (root !== path.resolve(fixtureRoot)) {
    throw new Error("合成场景生成器返回了受控目录之外的路径");
  }
  return {
    scenario: summary.scenario,
    root,
    importDirectories: summary.import_directories.map((relative) => path.join(root, relative)),
    images: summary.images.map((relative) => path.join(root, relative)),
    manifest: path.join(root, summary.manifest),
    expectations: summary.expectations,
  };
}

export async function cleanupSuccessfulScenario(environment: ScenarioEnvironment): Promise<void> {
  await removeScenarioDirectory(environment.externalRunRoot, environment.exportedBackupDir);
  await removeScenarioDirectory(environment.runRoot, environment.scenarioRoot);
  await removeEmptyDirectory(path.dirname(environment.exportedBackupDir));
  await removeEmptyDirectory(environment.externalRunRoot);
  await removeEmptyDirectory(environment.runRoot);
}

async function removeEmptyDirectory(directory: string): Promise<void> {
  try {
    await rmdir(directory);
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    if (code !== "ENOENT" && code !== "ENOTEMPTY" && code !== "EEXIST") throw error;
  }
}

export async function clearApplicationDataPreservingExternalBackup(
  environment: ScenarioEnvironment,
  backupZipPath: string,
  servicesStopped: boolean,
): Promise<void> {
  if (!servicesStopped) {
    throw new Error("只有在服务和 worker 全部停止后才能清空场景应用数据");
  }
  const [exportRoot, backupPath, scenarioRoot] = await Promise.all([
    realpath(environment.exportedBackupDir),
    realpath(backupZipPath),
    realpath(environment.scenarioRoot),
  ]);
  const backupRelative = path.relative(exportRoot, backupPath);
  if (!backupRelative || backupRelative.startsWith("..") || path.isAbsolute(backupRelative)) {
    throw new Error("备份 ZIP 必须位于当前 run-id 的受控外置备份目录");
  }
  const scenarioRelative = path.relative(scenarioRoot, backupPath);
  if (!scenarioRelative || (!scenarioRelative.startsWith("..") && !path.isAbsolute(scenarioRelative))) {
    throw new Error("外置备份 ZIP 不得位于待清空的场景应用目录内");
  }
  const backupStat = await stat(backupPath);
  if (!backupStat.isFile() || backupStat.size <= 0) {
    throw new Error("外置备份 ZIP 不存在或为空");
  }
  const beforeHash = createHash("sha256").update(await readFile(backupPath)).digest("hex");

  const preResetEvidenceRoot = path.join(environment.scenarioRoot, "pre-reset-evidence");
  const archivedLogsDir = path.join(preResetEvidenceRoot, "logs");
  const archivedDatabaseDir = path.join(preResetEvidenceRoot, "database");
  await mkdir(preResetEvidenceRoot);
  await Promise.all([mkdir(archivedLogsDir), mkdir(archivedDatabaseDir)]);
  const logEntries = (await readdir(environment.logsDir, { withFileTypes: true }))
    .filter((entry) => entry.isFile())
    .sort((left, right) => left.name.localeCompare(right.name));
  for (const requiredLog of ["fastapi.stdout.log", "fastapi.stderr.log", "app.log"]) {
    if (!logEntries.some((entry) => entry.name === requiredLog)) {
      throw new Error(`清空应用数据前缺少必要服务日志：${requiredLog}`);
    }
  }
  for (const entry of logEntries) {
    await copyFileVerified(
      path.join(environment.logsDir, entry.name),
      path.join(archivedLogsDir, entry.name),
    );
  }
  const databaseCandidates = [
    environment.databasePath,
    `${environment.databasePath}-wal`,
    `${environment.databasePath}-shm`,
  ];
  let databaseCopied = false;
  for (const source of databaseCandidates) {
    try {
      const sourceStat = await stat(source);
      if (!sourceStat.isFile()) continue;
    } catch {
      continue;
    }
    await copyFileVerified(source, path.join(archivedDatabaseDir, path.basename(source)));
    databaseCopied ||= source === environment.databasePath;
  }
  if (!databaseCopied) {
    throw new Error("清空应用数据前 SQLite 主数据库不存在");
  }

  for (const target of [environment.dataDir, environment.uploadsDir, environment.tempDir]) {
    await removeScenarioDirectory(environment.scenarioRoot, target);
  }
  await Promise.all([
    mkdir(environment.logsDir, { recursive: true }),
    mkdir(environment.backupDir, { recursive: true }),
    mkdir(environment.ocrResultsDir, { recursive: true }),
    mkdir(environment.configDir, { recursive: true }),
    mkdir(environment.uploadsDir, { recursive: true }),
    mkdir(environment.tempDir, { recursive: true }),
  ]);

  const afterBytes = await readFile(backupPath);
  const afterHash = createHash("sha256").update(afterBytes).digest("hex");
  if (afterBytes.length !== backupStat.size || afterHash !== beforeHash) {
    throw new Error("清空应用数据后外置备份 ZIP 完整性校验失败");
  }
}

async function copyFileVerified(source: string, target: string): Promise<void> {
  const sourceBytes = await readFile(source);
  await copyFile(source, target, constants.COPYFILE_EXCL);
  const targetBytes = await readFile(target);
  const sourceHash = createHash("sha256").update(sourceBytes).digest("hex");
  const targetHash = createHash("sha256").update(targetBytes).digest("hex");
  if (sourceBytes.length !== targetBytes.length || sourceHash !== targetHash) {
    throw new Error(`停服证据复制校验失败：${path.basename(source)}`);
  }
}

export interface ScenarioHarness {
  environment: ScenarioEnvironment;
  data: ScenarioData;
  server: ServerHandle;
  browser: Browser;
  context: BrowserContext;
  page: Page;
  diagnostics: PageDiagnostics;
  stopService(): Promise<StopReport>;
  startService(): Promise<ServerHandle>;
  replaceBrowserContext(): Promise<Page>;
  restartService(options?: { freshBrowserContext?: boolean }): Promise<ServerHandle>;
  clearApplicationDataPreservingExternalBackup(backupZipPath: string): Promise<void>;
}

export interface BrowserRuntime {
  browser: Browser;
  context: BrowserContext;
  page: Page;
  diagnostics: PageDiagnostics;
  browserProcesses: TrackedProcessIdentity[];
}

interface BrowserAcquisitionOptions {
  snapshot?: () => Promise<TrackedProcessIdentity[]>;
  cleanup?: (identities: TrackedProcessIdentity[]) => Promise<number[]>;
  existing?: (identities: TrackedProcessIdentity[]) => Promise<TrackedProcessIdentity[]>;
  stop?: () => Promise<StopReport>;
}

export async function acquireBrowserRuntime(
  server: ServerHandle,
  playwright: Playwright,
  launcher: (playwright: Playwright) => Promise<Browser> = launchBrowser,
  options: BrowserAcquisitionOptions = {},
): Promise<BrowserRuntime> {
  const snapshot = options.snapshot ?? (() => snapshotDescendantProcesses(server.environment));
  const cleanup =
    options.cleanup ??
    ((identities: TrackedProcessIdentity[]) =>
      cleanupRecordedBrowserProcesses(server.environment, identities));
  const existing =
    options.existing ??
    ((identities: TrackedProcessIdentity[]) => existingIdentities(server.environment, identities));
  const before = await snapshot();
  let browser: Browser | undefined;
  let context: BrowserContext | undefined;
  try {
    browser = await launcher(playwright);
    context = await browser.newContext({ baseURL: server.baseUrl, acceptDownloads: true });
    const page = await context.newPage();
    const diagnostics = attachPageDiagnostics(page);
    await context.tracing.start({ screenshots: true, snapshots: true, sources: true });
    const after = await snapshot();
    const baseline = new Set(before.map(processKey));
    const browserProcesses = after.filter((identity) => !baseline.has(processKey(identity)));
    if (!browserProcesses.length) {
      throw new Error("未能记录 Playwright 浏览器进程树");
    }
    return { browser, context, page, diagnostics, browserProcesses };
  } catch (error) {
    const cleanupErrors: string[] = [];
    await context?.close().catch((cleanupError) => {
      cleanupErrors.push(`浏览器上下文关闭失败：${String(cleanupError)}`);
    });
    await browser?.close().catch((cleanupError) => {
      cleanupErrors.push(`浏览器关闭失败：${String(cleanupError)}`);
    });
    const afterFailure = await snapshot().catch((cleanupError) => {
      cleanupErrors.push(`浏览器进程快照失败：${String(cleanupError)}`);
      return [];
    });
    const baseline = new Set(before.map(processKey));
    const captured = afterFailure.filter((identity) => !baseline.has(processKey(identity)));
    await cleanup(captured).catch((cleanupError) => {
      cleanupErrors.push(`浏览器进程清理失败：${String(cleanupError)}`);
    });
    const remaining = await existing(captured).catch((cleanupError) => {
      cleanupErrors.push(`浏览器进程复核失败：${String(cleanupError)}`);
      return captured;
    });
    if (remaining.length) {
      cleanupErrors.push(`浏览器仍有残留 PID：${remaining.map((identity) => identity.pid).join(",")}`);
    }
    const report = await (options.stop ? options.stop() : stopServer(server)).catch((stopError) => {
      cleanupErrors.push(`服务清理异常：${String(stopError)}`);
      return undefined;
    });
    if (
      !report ||
      !report.mainExited ||
      !report.childrenExited ||
      !report.healthUnreachable ||
      !report.portReleased ||
      report.remainingPids.length
    ) {
      cleanupErrors.push(`服务仍有残留：${JSON.stringify(report)}`);
    }
    if (cleanupErrors.length) {
      const remainingPids = remaining.map((identity) => identity.pid);
      throw new ResourceCleanupError(
        "browser",
        false,
        remainingPids,
        `浏览器初始化失败且清理未完成（残留 PID：${remainingPids.join(",") || "未知"}）：${cleanupErrors.join("；")}`,
        error,
      );
    }
    throw error;
  }
}

interface ScenarioPrerequisiteDependencies {
  createEnvironment?: typeof createScenarioEnvironment;
  prepareData?: typeof prepareScenarioData;
  start?: typeof startServer;
  acquireBrowser?: typeof acquireBrowserRuntime;
  stop?: typeof stopServer;
}

export async function acquireScenarioPrerequisites(
  runId: string,
  scenarioName: ScenarioName,
  playwright: Playwright,
  dependencies: ScenarioPrerequisiteDependencies = {},
): Promise<{
  environment: ScenarioEnvironment;
  data: ScenarioData;
  server: ServerHandle;
  runtime: BrowserRuntime;
}> {
  const createEnvironment = dependencies.createEnvironment ?? createScenarioEnvironment;
  const prepareData = dependencies.prepareData ?? prepareScenarioData;
  const start = dependencies.start ?? startServer;
  const acquireBrowser = dependencies.acquireBrowser ?? acquireBrowserRuntime;
  const stop = dependencies.stop ?? stopServer;
  let environment: ScenarioEnvironment | undefined;
  let server: ServerHandle | undefined;
  try {
    environment = await createEnvironment(runId, scenarioName);
    const data = await prepareData(environment);
    server = await start(environment);
    const runtime = await acquireBrowser(server, playwright);
    return { environment, data, server, runtime };
  } catch (error) {
    const cleanupErrors: string[] = [];
    const remainingPids: number[] = [];
    if (error instanceof ResourceCleanupError && !error.cleanupVerified) {
      cleanupErrors.push(`${error.resource} 清理状态未验证：${error.message}`);
      remainingPids.push(...error.remainingPids);
    }
    if (server) {
      const report = await stop(server).catch((stopError) => {
        cleanupErrors.push(`服务清理失败：${String(stopError)}`);
        return undefined;
      });
      if (
        !report ||
        !report.mainExited ||
        !report.childrenExited ||
        !report.healthUnreachable ||
        !report.portReleased ||
        report.remainingPids.length
      ) {
        cleanupErrors.push(`服务残留：${JSON.stringify(report)}`);
        remainingPids.push(...(report?.remainingPids ?? []));
      }
    }
    if (environment && cleanupErrors.length === 0) {
      await cleanupSuccessfulScenario(environment).catch((cleanupError) => {
        cleanupErrors.push(`隔离目录清理失败：${String(cleanupError)}`);
      });
    }
    if (cleanupErrors.length) {
      const uniquePids = [...new Set(remainingPids)];
      throw new ResourceCleanupError(
        "scenario",
        false,
        uniquePids,
        `场景初始化失败且清理未完成（残留 PID：${uniquePids.join(",") || "未知"}）：${cleanupErrors.join("；")}`,
        error,
      );
    }
    throw error;
  }
}

export interface BrowserStopReport {
  forcedPids: number[];
  remainingPids: number[];
}

export async function closeBrowserRuntime(
  runtime: BrowserRuntime,
  environment: ScenarioEnvironment,
): Promise<BrowserStopReport> {
  await runtime.context.close().catch(() => undefined);
  await runtime.browser.close().catch(() => undefined);
  const deadline = Date.now() + 5_000;
  let remaining = await existingIdentities(environment, runtime.browserProcesses);
  while (remaining.length && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 25));
    remaining = await existingIdentities(environment, runtime.browserProcesses);
  }
  const forcedPids = await cleanupRecordedBrowserProcesses(environment, remaining);
  remaining = await existingIdentities(environment, runtime.browserProcesses);
  return { forcedPids, remainingPids: remaining.map((identity) => identity.pid) };
}

function processKey(identity: Pick<TrackedProcessIdentity, "pid" | "startedAt">): string {
  return `${identity.pid}:${identity.startedAt.toFixed(3)}`;
}

async function existingIdentities(
  environment: ScenarioEnvironment,
  identities: TrackedProcessIdentity[],
): Promise<TrackedProcessIdentity[]> {
  const states = await Promise.all(
    identities.map((identity) =>
      recordedIdentityExists(environment, identity).catch(() => true),
    ),
  );
  return identities.filter((_, index) => states[index]);
}

async function cleanupRecordedBrowserProcesses(
  environment: ScenarioEnvironment,
  identities: TrackedProcessIdentity[],
): Promise<number[]> {
  const identityPids = new Set(identities.map((identity) => identity.pid));
  const roots = identities.filter((identity) => !identityPids.has(identity.parentPid));
  const forced: number[] = [];
  for (const identity of roots) {
    if (await recordedIdentityExists(environment, identity).catch(() => false)) {
      await terminateRecordedIdentity(environment, identity);
      forced.push(identity.pid);
    }
  }
  return forced;
}

type ScenarioFixtures = {
  scenarioName: ScenarioName;
  scenario: ScenarioHarness;
};

export const test = base.extend<ScenarioFixtures>({
  scenarioName: ["A", { option: true }],
  scenario: async ({ playwright, scenarioName }, use, testInfo) => {
    const runId = process.env.E2E_RUN_ID || createRunId();
    const prerequisites = await acquireScenarioPrerequisites(runId, scenarioName, playwright);
    const { environment, data, runtime: initialRuntime } = prerequisites;
    let server = prerequisites.server;
    const browser = initialRuntime.browser;
    let context = initialRuntime.context;
    let page = initialRuntime.page;
    let diagnostics = initialRuntime.diagnostics;
    let traceSequence = 1;
    const tracePaths: string[] = [];
    let tracing = true;
    let serviceRunning = true;
    let lastStopReport: StopReport | undefined;

    const harness: ScenarioHarness = {
      environment,
      data,
      server,
      browser,
      context,
      page,
      diagnostics,
      async stopService() {
        if (!serviceRunning) throw new Error("场景服务已经停止");
        lastStopReport = await stopServer(server);
        serviceRunning = false;
        if (
          !lastStopReport.mainExited ||
          !lastStopReport.childrenExited ||
          !lastStopReport.healthUnreachable ||
          !lastStopReport.portReleased ||
          lastStopReport.remainingPids.length
        ) {
          throw new Error(`场景服务未完全停止：${JSON.stringify(lastStopReport)}`);
        }
        return lastStopReport;
      },
      async startService() {
        if (serviceRunning) throw new Error("场景服务仍在运行");
        server = await startServer(environment);
        serviceRunning = true;
        harness.server = server;
        return server;
      },
      async replaceBrowserContext() {
        if (tracing) {
          const segmentPath = path.join(environment.scenarioRoot, `trace-${traceSequence}.zip`);
          await context.tracing.stop({ path: segmentPath });
          tracePaths.push(segmentPath);
          tracing = false;
          traceSequence += 1;
        }
        await context.close();
        context = await browser.newContext({ baseURL: server.baseUrl, acceptDownloads: true });
        await context.tracing.start({ screenshots: true, snapshots: true, sources: true });
        tracing = true;
        page = await context.newPage();
        attachPageDiagnostics(page, diagnostics);
        harness.context = context;
        harness.page = page;
        return page;
      },
      async restartService(options = {}) {
        await harness.stopService();
        await harness.startService();
        if (options.freshBrowserContext) await harness.replaceBrowserContext();
        return server;
      },
      async clearApplicationDataPreservingExternalBackup(backupZipPath) {
        await clearApplicationDataPreservingExternalBackup(
          environment,
          backupZipPath,
          !serviceRunning,
        );
      },
    };

    let pageHtml = "<html><body>页面内容捕获失败</body></html>";
    let teardownError: unknown;
    await use(harness);
    const failed = testInfo.status !== testInfo.expectedStatus;
    const tracePath = path.join(environment.scenarioRoot, "trace.zip");
    const screenshotPath = path.join(environment.screenshotsDir, "final.png");
    try {
      if (failed) {
        pageHtml = await page.content();
        await page.screenshot({ path: screenshotPath, fullPage: true });
      }
      if (tracing) {
        await context.tracing.stop({ path: tracePath });
        tracePaths.push(tracePath);
        tracing = false;
      }
    } catch (error) {
      teardownError = error;
    }
    const browserStop = await closeBrowserRuntime(
      {
        browser,
        context,
        page,
        diagnostics,
        browserProcesses: initialRuntime.browserProcesses,
      },
      environment,
    ).catch((error) => {
      teardownError ??= error;
      return { forcedPids: [], remainingPids: [-1] };
    });
    if (browserStop.remainingPids.length) {
      teardownError ??= new Error(
        `浏览器进程未完全退出：${browserStop.remainingPids.join(",")}`,
      );
    }
    if (serviceRunning) {
      try {
        lastStopReport = await stopServer(server);
        serviceRunning = false;
      } catch (error) {
        teardownError ??= error;
      }
    }

    if (failed || teardownError) {
      if (!lastStopReport) {
        throw new Error("场景失败但没有可验证的服务停止报告，已保留运行目录", {
          cause: teardownError,
        });
      }
      await mkdir(environment.screenshotsDir, { recursive: true });
      await collectFailureEvidence({
        environment,
        stopReport: lastStopReport,
        tracePath,
        tracePaths,
        screenshotPath,
        html: pageHtml,
        consoleMessages: diagnostics.consoleMessages,
        pageErrors: diagnostics.pageErrors,
        requestFailures: diagnostics.requestFailures,
        configuration: {
          runId,
          scenario: scenarioName,
          host: server.host,
          port: server.port,
          dataDir: environment.dataDir,
          backupDir: environment.backupDir,
          ocrResultsDir: environment.ocrResultsDir,
          uploadsDir: environment.uploadsDir,
          configDir: environment.configDir,
          OCR_ENGINE: "mock",
        },
        browser: {
          name: (process.env.E2E_BROWSER || "edge") === "edge" ? "Microsoft Edge" : "Chromium",
          version: browser.version(),
        },
        redactionContext: {
          workspacePath: environment.repoRoot,
          tempPath: environment.scenarioRoot,
          syntheticFilenames: data.images.map((image) => path.basename(image)),
        },
      });
      if (teardownError) throw teardownError;
      return;
    }
    if (
      !lastStopReport?.mainExited ||
      !lastStopReport.childrenExited ||
      !lastStopReport.healthUnreachable ||
      !lastStopReport.portReleased ||
      lastStopReport.remainingPids.length
    ) {
      throw new Error(`成功场景仍有残留进程或端口：${JSON.stringify(lastStopReport)}`);
    }
    await cleanupSuccessfulScenario(environment);
  },
});

export { expect } from "@playwright/test";
