import { createHash } from "node:crypto";
import { constants, existsSync } from "node:fs";
import { copyFile, mkdir, readFile, readdir, stat, writeFile } from "node:fs/promises";
import path from "node:path";

import type { StopReport } from "./server";
import { removeScenarioDirectory, type ScenarioEnvironment } from "./environment";
import { redactText, type RedactionContext } from "./redaction";

export interface EvidenceFile {
  name: string;
  path: string;
  sha256: string;
  size: number;
}

export interface DatabaseEvidenceInput {
  databasePath: string;
  evidenceDir: string;
  stopReport: StopReport;
  targetPrefix?: string;
}

export interface EvidenceInput {
  environment: ScenarioEnvironment;
  stopReport: StopReport;
  tracePath: string;
  tracePaths?: string[];
  screenshotPath: string;
  html: string;
  consoleMessages: string[];
  pageErrors: string[];
  requestFailures: string[];
  configuration: Record<string, unknown>;
  browser: { name: string; version: string };
  redactionContext: RedactionContext;
}

export interface EvidenceManifest {
  evidenceDir: string;
  manifestPath: string;
  files: EvidenceFile[];
  missingArtifacts: Array<{ name: string; reason: string }>;
}

export async function collectDatabaseEvidence(
  input: DatabaseEvidenceInput,
): Promise<EvidenceFile[]> {
  if (
    !input.stopReport.mainExited ||
    !input.stopReport.childrenExited ||
    !input.stopReport.healthUnreachable ||
    !input.stopReport.portReleased ||
    input.stopReport.remainingPids.length > 0
  ) {
    throw new Error("服务和 worker 尚未全部停止，拒绝复制 SQLite 证据");
  }
  const candidates = [input.databasePath, `${input.databasePath}-wal`, `${input.databasePath}-shm`];
  try {
    await stat(input.databasePath);
  } catch {
    throw new Error("SQLite 主数据库不存在");
  }
  await mkdir(input.evidenceDir, { recursive: true });
  const collected: EvidenceFile[] = [];
  for (const source of candidates) {
    let sourceStat;
    try {
      sourceStat = await stat(source);
    } catch {
      continue;
    }
    if (!sourceStat.isFile()) continue;
    const name = `${input.targetPrefix ?? ""}${path.basename(source)}`;
    const target = path.join(input.evidenceDir, name);
    await copyFile(source, target, constants.COPYFILE_EXCL);
    const [sourceBytes, targetBytes] = await Promise.all([readFile(source), readFile(target)]);
    const sourceHash = createHash("sha256").update(sourceBytes).digest("hex");
    const targetHash = createHash("sha256").update(targetBytes).digest("hex");
    if (sourceHash !== targetHash) {
      throw new Error(`SQLite 证据复制校验失败：${name}`);
    }
    collected.push({ name, path: target, sha256: targetHash, size: targetBytes.length });
  }
  return collected;
}

export async function collectFailureEvidence(_input: EvidenceInput): Promise<EvidenceManifest> {
  const input = _input;
  const evidenceDir = path.join(
    input.environment.e2eRoot,
    "artifacts",
    input.environment.runId,
    input.environment.scenarioName,
  );
  const runtimeContext: RedactionContext = {
    userHome: process.env.USERPROFILE,
    workspacePath: input.environment.repoRoot,
    tempPath: input.environment.scenarioRoot,
    privateFilenames: input.redactionContext.privateFilenames,
    syntheticFilenames: input.redactionContext.syntheticFilenames,
  };
  const clean = (value: string): string => {
    const redacted = redactText(redactText(value, input.redactionContext), runtimeContext);
    assertNoSensitiveText(redacted, input.redactionContext);
    assertNoSensitiveText(redacted, runtimeContext);
    return redacted;
  };
  let evidenceVerified = false;
  try {
    await mkdir(path.dirname(evidenceDir), { recursive: true });
    await mkdir(evidenceDir, { recursive: false });
    const files: EvidenceFile[] = [];
    const missingArtifacts: Array<{ name: string; reason: string }> = [];
    const tracePaths = [
      ...new Set(input.tracePaths?.length ? input.tracePaths : [input.tracePath]),
    ];
    for (const [index, traceSource] of tracePaths.entries()) {
      const sourceName = path.basename(traceSource);
      const targetName = /^trace(?:-\d+)?\.zip$/i.test(sourceName)
        ? sourceName
        : index === tracePaths.length - 1
          ? "trace.zip"
          : `trace-${index + 1}.zip`;
      await copyOptionalBinary(
        traceSource,
        path.join(evidenceDir, targetName),
        files,
        missingArtifacts,
      );
    }
    await copyOptionalBinary(
      input.screenshotPath,
      path.join(evidenceDir, "final.png"),
      files,
      missingArtifacts,
    );
    files.push(await writeVerifiedText(path.join(evidenceDir, "page.html"), clean(input.html)));
    files.push(
      await writeVerifiedText(
        path.join(evidenceDir, "console.log"),
        clean(logBody(input.consoleMessages)),
      ),
    );
    files.push(
      await writeVerifiedText(
        path.join(evidenceDir, "pageerror.log"),
        clean(logBody(input.pageErrors)),
      ),
    );
    files.push(
      await writeVerifiedText(
        path.join(evidenceDir, "requestfailed.log"),
        clean(logBody(input.requestFailures)),
      ),
    );

    const preResetEvidenceRoot = path.join(input.environment.scenarioRoot, "pre-reset-evidence");
    const archivedLogsDir = path.join(preResetEvidenceRoot, "logs");
    const archivedDatabasePath = path.join(preResetEvidenceRoot, "database", "app.db");
    const currentLogEntries = await readLogEntries(input.environment.logsDir);
    const archivedLogEntries = await readLogEntries(archivedLogsDir);
    for (const requiredLog of ["fastapi.stdout.log", "fastapi.stderr.log", "app.log"]) {
      if (
        !currentLogEntries.some((entry) => entry.name === requiredLog) &&
        !archivedLogEntries.some((entry) => entry.name === requiredLog)
      ) {
        throw new Error(`缺少必要服务日志：${requiredLog}`);
      }
    }
    for (const [logsDir, prefix, entries] of [
      [input.environment.logsDir, "", currentLogEntries],
      [archivedLogsDir, "pre-reset-", archivedLogEntries],
    ] as const) {
      for (const entry of entries) {
        const source = path.join(logsDir, entry.name);
        const contents = await readFile(source, "utf8");
        files.push(
          await writeVerifiedText(path.join(evidenceDir, `${prefix}${entry.name}`), clean(contents)),
        );
      }
    }

    const configuration = sanitizeConfiguration(input.configuration);
    files.push(
      await writeVerifiedText(
        path.join(evidenceDir, "environment.json"),
        clean(`${JSON.stringify(configuration, null, 2)}\n`),
      ),
    );
    files.push(
      await writeVerifiedText(
        path.join(evidenceDir, "browser.json"),
        `${JSON.stringify(input.browser, null, 2)}\n`,
      ),
    );
    let databaseEvidenceCount = 0;
    for (const [databasePath, targetPrefix] of [
      [input.environment.databasePath, ""],
      [archivedDatabasePath, "pre-reset-"],
    ] as const) {
      if (!existsSync(databasePath)) continue;
      const databaseFiles = await collectDatabaseEvidence({
        databasePath,
        evidenceDir,
        stopReport: input.stopReport,
        targetPrefix,
      });
      databaseEvidenceCount += databaseFiles.length;
      files.push(...databaseFiles);
    }
    if (!databaseEvidenceCount) throw new Error("SQLite 主数据库不存在");

    for (const file of files) {
      await verifyEvidenceFile(file);
    }
    const manifestPath = path.join(evidenceDir, "manifest.json");
    const manifestPayload = {
      schemaVersion: 1,
      runId: input.environment.runId,
      scenario: input.environment.scenarioName,
      collectedAt: new Date().toISOString(),
      browser: input.browser,
      files: files.map((file) => ({
        name: file.name,
        sha256: file.sha256,
        size: file.size,
      })),
      missingArtifacts,
    };
    await writeFile(manifestPath, `${JSON.stringify(manifestPayload, null, 2)}\n`, {
      encoding: "utf8",
      flag: "wx",
    });
    const persistedManifest = JSON.parse(await readFile(manifestPath, "utf8")) as {
      files: Array<{ name: string; sha256: string; size: number }>;
    };
    if (persistedManifest.files.length !== files.length) {
      throw new Error("证据清单校验失败");
    }
    evidenceVerified = true;
    if (existsSync(input.environment.exportedBackupDir)) {
      await removeScenarioDirectory(
        input.environment.externalRunRoot,
        input.environment.exportedBackupDir,
      );
    }
    await removeScenarioDirectory(input.environment.runRoot, input.environment.scenarioRoot);
    return { evidenceDir, manifestPath, files, missingArtifacts };
  } catch (error) {
    const rawError = error instanceof Error ? error.stack || error.message : String(error);
    if (existsSync(input.environment.scenarioRoot)) {
      await writeFile(
        path.join(input.environment.scenarioRoot, "evidence-collection-error.raw.log"),
        `${rawError}\n`,
        { encoding: "utf8", flag: "a" },
      ).catch(() => undefined);
    }
    const reportedPath = redactText(input.environment.scenarioRoot, {
      workspacePath: input.environment.repoRoot,
      tempPath: input.environment.scenarioRoot,
    });
    const safeError = redactText(
      redactText(rawError, input.redactionContext),
      runtimeContext,
    );
    throw new Error(
      evidenceVerified
        ? `故障证据已校验，但临时目录清理未完成 ${reportedPath}：${safeError}`
        : `失败证据收集失败；保留原始临时目录 ${reportedPath}：${safeError}`,
    );
  }
}

function logBody(lines: string[]): string {
  return `${lines.length ? lines.join("\n") : "(none)"}\n`;
}

async function readLogEntries(directory: string) {
  try {
    return (await readdir(directory, { withFileTypes: true }))
      .filter((entry) => entry.isFile())
      .sort((left, right) => left.name.localeCompare(right.name));
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return [];
    throw error;
  }
}

function sha256(bytes: Buffer): string {
  return createHash("sha256").update(bytes).digest("hex");
}

async function writeVerifiedText(target: string, contents: string): Promise<EvidenceFile> {
  await writeFile(target, contents || "(empty)\n", { encoding: "utf8", flag: "wx" });
  const bytes = await readFile(target);
  return { name: path.basename(target), path: target, sha256: sha256(bytes), size: bytes.length };
}

async function copyVerifiedBinary(source: string, target: string): Promise<EvidenceFile> {
  const sourceBytes = await readFile(source);
  await copyFile(source, target, constants.COPYFILE_EXCL);
  const targetBytes = await readFile(target);
  if (sha256(sourceBytes) !== sha256(targetBytes)) {
    throw new Error(`二进制证据复制校验失败：${path.basename(target)}`);
  }
  return {
    name: path.basename(target),
    path: target,
    sha256: sha256(targetBytes),
    size: targetBytes.length,
  };
}

async function copyOptionalBinary(
  source: string,
  target: string,
  files: EvidenceFile[],
  missingArtifacts: Array<{ name: string; reason: string }>,
): Promise<void> {
  try {
    files.push(await copyVerifiedBinary(source, target));
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      missingArtifacts.push({ name: path.basename(target), reason: "source-missing" });
      return;
    }
    throw error;
  }
}

async function verifyEvidenceFile(file: EvidenceFile): Promise<void> {
  const bytes = await readFile(file.path);
  if (bytes.length !== file.size || sha256(bytes) !== file.sha256) {
    throw new Error(`证据哈希复核失败：${file.name}`);
  }
}

function sanitizeConfiguration(value: unknown, key = ""): unknown {
  if (/authorization|cookie|token|secret|password|api[_-]?key/i.test(key)) {
    return "<redacted>";
  }
  if (Array.isArray(value)) return value.map((item) => sanitizeConfiguration(item));
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([childKey, child]) => [
        childKey,
        sanitizeConfiguration(child, childKey),
      ]),
    );
  }
  return value;
}

function assertNoSensitiveText(text: string, context: RedactionContext): void {
  const forbidden = [
    context.userHome,
    context.workspacePath,
    context.tempPath,
    ...(context.privateFilenames ?? []).filter(
      (filename) => !(context.syntheticFilenames ?? []).includes(filename),
    ),
  ].filter((value): value is string => Boolean(value));
  for (const value of forbidden) {
    if (text.toLocaleLowerCase().includes(value.toLocaleLowerCase())) {
      throw new Error("脱敏扫描仍发现路径或私有文件名");
    }
  }
  const unredactedHeader = /\b(?:authorization|(?:set-)?cookie)\s*:\s*(?!<redacted>)[^\r\n]+/i;
  const unredactedJsonHeader = /["'](?:authorization|(?:set-)?cookie)["']\s*:\s*["'](?!<redacted>)/i;
  const unredactedSecret = /\b(?:access[_-]?token|api[_-]?key|secret|password)\s*[:=]\s*(?!<redacted>)[^\s]+/i;
  if (
    unredactedHeader.test(text) ||
    unredactedJsonHeader.test(text) ||
    unredactedSecret.test(text)
  ) {
    throw new Error("脱敏扫描仍发现凭据或敏感请求头");
  }
}
