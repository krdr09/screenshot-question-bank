import { mkdir, realpath, rm, stat } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

export type ScenarioName = "A" | "B" | "C" | "D" | "E";

export interface ScenarioEnvironment {
  runId: string;
  scenarioName: ScenarioName;
  repoRoot: string;
  e2eRoot: string;
  runRoot: string;
  externalRunRoot: string;
  scenarioRoot: string;
  dataDir: string;
  databasePath: string;
  databaseUrl: string;
  logsDir: string;
  screenshotsDir: string;
  backupDir: string;
  exportedBackupDir: string;
  ocrResultsDir: string;
  uploadsDir: string;
  configDir: string;
  tempDir: string;
  controlFile: string;
  mockOcrManifestPath: string;
  frontendDist: string;
}

export async function createScenarioEnvironment(
  runId: string,
  scenarioName: ScenarioName,
  options?: { e2eRoot?: string; frontendDist?: string; mkdir?: typeof mkdir },
): Promise<ScenarioEnvironment> {
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,100}$/.test(runId) || runId === "." || runId === "..") {
    throw new Error("run-id 格式无效");
  }
  if (!(["A", "B", "C", "D", "E"] as const).includes(scenarioName)) {
    throw new Error("E2E 场景名称无效");
  }

  const repoRoot = path.resolve(fileURLToPath(new URL("../..", import.meta.url)));
  const e2eRoot = path.resolve(options?.e2eRoot ?? path.join(repoRoot, "e2e"));
  const controlRoot = options?.e2eRoot ? e2eRoot : repoRoot;
  const makeDirectory = options?.mkdir ?? mkdir;
  const runRoot = path.join(controlRoot, ".e2e-tmp", runId);
  const externalRunRoot = path.join(controlRoot, ".e2e-runs", runId);
  const scenarioRoot = path.join(runRoot, scenarioName);
  const dataDir = path.join(scenarioRoot, "data");
  const databasePath = path.join(dataDir, "app.db");
  const environment: ScenarioEnvironment = {
    runId,
    scenarioName,
    repoRoot,
    e2eRoot,
    runRoot,
    externalRunRoot,
    scenarioRoot,
    dataDir,
    databasePath,
    databaseUrl: `sqlite:///${databasePath.replaceAll("\\", "/")}`,
    logsDir: path.join(dataDir, "logs"),
    screenshotsDir: path.join(scenarioRoot, "screenshots"),
    backupDir: path.join(dataDir, "backups"),
    exportedBackupDir: path.join(externalRunRoot, "external", scenarioName),
    ocrResultsDir: path.join(dataDir, "ocr-results"),
    uploadsDir: path.join(scenarioRoot, "uploads"),
    configDir: path.join(dataDir, "config"),
    tempDir: path.join(scenarioRoot, "temp"),
    controlFile: path.join(scenarioRoot, "server-control.json"),
    mockOcrManifestPath: path.join(scenarioRoot, "fixtures", "fixture.json"),
    frontendDist: path.resolve(options?.frontendDist ?? path.join(repoRoot, "frontend", "dist")),
  };
  let scenarioCreated = false;
  let externalCreated = false;
  try {
    await makeDirectory(path.dirname(environment.scenarioRoot), { recursive: true });
    try {
      await makeDirectory(environment.scenarioRoot);
      scenarioCreated = true;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "EEXIST") {
        throw new Error("场景目录已存在，拒绝复用非干净环境");
      }
      throw error;
    }
    await makeDirectory(path.dirname(environment.exportedBackupDir), { recursive: true });
    try {
      await makeDirectory(environment.exportedBackupDir);
      externalCreated = true;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "EEXIST") {
        throw new Error("场景外置备份目录已存在，拒绝复用非干净环境");
      }
      throw error;
    }
    for (const directory of [
      environment.dataDir,
      environment.logsDir,
      environment.screenshotsDir,
      environment.backupDir,
      environment.ocrResultsDir,
      environment.uploadsDir,
      environment.configDir,
      environment.tempDir,
      path.dirname(environment.mockOcrManifestPath),
    ]) {
      await makeDirectory(directory, { recursive: true });
    }
  } catch (error) {
    const cleanupErrors: string[] = [];
    if (externalCreated) {
      await removeScenarioDirectory(
        environment.externalRunRoot,
        environment.exportedBackupDir,
      ).catch((cleanupError) => cleanupErrors.push(String(cleanupError)));
    }
    if (scenarioCreated) {
      await removeScenarioDirectory(environment.runRoot, environment.scenarioRoot).catch(
        (cleanupError) => cleanupErrors.push(String(cleanupError)),
      );
    }
    if (cleanupErrors.length) {
      throw new Error(`场景环境创建失败且目录清理未完成：${cleanupErrors.join("；")}`, {
        cause: error,
      });
    }
    throw error;
  }
  return environment;
}

export function buildChildEnvironment(
  scenario: ScenarioEnvironment,
  parent: NodeJS.ProcessEnv = process.env,
): NodeJS.ProcessEnv {
  const required = ["SystemRoot", "COMSPEC", "PATH"] as const;
  for (const key of required) {
    if (!parent[key]) {
      throw new Error(`子进程环境缺少必要变量 ${key}`);
    }
  }
  const allowed = [
    "SystemRoot",
    "WINDIR",
    "COMSPEC",
    "PATH",
    "PATHEXT",
    "NUMBER_OF_PROCESSORS",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
  ] as const;
  const child: NodeJS.ProcessEnv = {};
  for (const key of allowed) {
    if (parent[key] !== undefined) {
      child[key] = parent[key];
    }
  }
  return {
    ...child,
    TEMP: scenario.tempDir,
    TMP: scenario.tempDir,
    PYTHONPATH: path.join(scenario.repoRoot, "backend"),
    PYTHONIOENCODING: "utf-8",
    QUESTION_PRACTICE_HOST: "127.0.0.1",
    QUESTION_PRACTICE_DATA_DIR: scenario.dataDir,
    QUESTION_PRACTICE_DATABASE_URL: scenario.databaseUrl,
    QUESTION_PRACTICE_AUTO_CREATE_SCHEMA: "false",
    QUESTION_PRACTICE_START_BACKGROUND_WORKER: "true",
    QUESTION_PRACTICE_MOCK_OCR_FIXTURE_PATH: scenario.mockOcrManifestPath,
    QUESTION_PRACTICE_FRONTEND_DIST: scenario.frontendDist,
    OCR_ENGINE: "mock",
  };
}

export async function removeScenarioDirectory(
  runRoot: string,
  target: string,
): Promise<void> {
  try {
    await stat(target);
  } catch {
    throw new Error("待删除的场景目录不存在");
  }
  const [verifiedRoot, verifiedTarget] = await Promise.all([
    realpath(runRoot),
    realpath(target),
  ]);
  const relative = path.relative(verifiedRoot, verifiedTarget);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error("拒绝删除 run-id 受控目录之外或 run 根目录本身");
  }
  await rm(verifiedTarget, { recursive: true, force: false });
}
