import { access, mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

import { test, expect } from "@playwright/test";

import { collectDatabaseEvidence, collectFailureEvidence } from "../support/evidence";
import { createScenarioEnvironment } from "../support/environment";
import { redactText } from "../support/redaction";
import type { StopReport } from "../support/server";

const stopped: StopReport = {
  graceful: true,
  forcedPids: [],
  errors: [],
  mainExited: true,
  childrenExited: true,
  healthUnreachable: true,
  portReleased: true,
  remainingPids: [],
};

test("redacts paths credentials cookies and private filenames", () => {
  const raw = [
    "C:\\Users\\alice\\work\\private题.png",
    "C:\\Users\\alice\\AppData\\Local\\Temp\\run-1",
    "Authorization: Bearer abc.def.ghi",
    "Cookie: sid=xyz; theme=dark",
    '"Authorization": "Bearer json-auth-secret"',
    '"Cookie": "sid=json-cookie"',
    "api_key=top-secret",
    "合成-A-01.png",
  ].join("\n");

  const clean = redactText(raw, {
    userHome: "C:\\Users\\alice",
    workspacePath: "C:\\Users\\alice\\work",
    tempPath: "C:\\Users\\alice\\AppData\\Local\\Temp",
    privateFilenames: ["private题.png"],
    syntheticFilenames: ["合成-A-01.png"],
  });

  expect(clean).not.toContain("alice");
  expect(clean).not.toContain("private题.png");
  expect(clean).not.toContain("abc.def.ghi");
  expect(clean).not.toContain("sid=xyz");
  expect(clean).not.toContain("json-auth-secret");
  expect(clean).not.toContain("json-cookie");
  expect(clean).not.toContain("top-secret");
  expect(clean).toContain("<private-file>");
  expect(clean).toContain("合成-A-01.png");
});

test("copies sqlite main WAL and SHM evidence only after the service stopped", async ({}, testInfo) => {
  const sourceDir = testInfo.outputPath("database-source");
  const evidenceDir = testInfo.outputPath("evidence");
  await mkdir(sourceDir, { recursive: true });
  const databasePath = path.join(sourceDir, "app.db");
  await writeFile(databasePath, "main");
  await writeFile(`${databasePath}-wal`, "wal");
  await writeFile(`${databasePath}-shm`, "shm");

  await expect(
    collectDatabaseEvidence({
      databasePath,
      evidenceDir,
      stopReport: { ...stopped, childrenExited: false },
    }),
  ).rejects.toThrow("服务和 worker 尚未全部停止");

  const files = await collectDatabaseEvidence({ databasePath, evidenceDir, stopReport: stopped });

  expect(files.map((file) => file.name)).toEqual(["app.db", "app.db-wal", "app.db-shm"]);
  expect(files.every((file) => file.size > 0 && file.sha256.length === 64)).toBeTruthy();
});

test("failure evidence is redacted hashed and verified before scenario cleanup", async ({}, testInfo) => {
  const environment = await createScenarioEnvironment("20260718T120000Z-run-abc", "A", {
    e2eRoot: testInfo.outputPath("e2e-root"),
  });
  const tracePath = path.join(environment.scenarioRoot, "trace.zip");
  const firstTracePath = path.join(environment.scenarioRoot, "trace-1.zip");
  const screenshotPath = path.join(environment.screenshotsDir, "final.png");
  await writeFile(tracePath, "trace-binary");
  await writeFile(firstTracePath, "trace-before-restart");
  await writeFile(screenshotPath, "png-binary");
  await writeFile(environment.databasePath, "database");
  await writeFile(
    path.join(environment.logsDir, "fastapi.stdout.log"),
    "Authorization: Bearer server-secret C:\\Users\\alice\\work\\private题.png",
  );
  await writeFile(path.join(environment.logsDir, "fastapi.stderr.log"), "pageerror secret=stderr-secret");
  await writeFile(path.join(environment.logsDir, "app.log"), "Cookie: sid=private-cookie");

  const manifest = await collectFailureEvidence({
    environment,
    stopReport: stopped,
    tracePath,
    tracePaths: [firstTracePath, tracePath],
    screenshotPath,
    html: '<html data-workspace="C:\\Users\\alice\\work">private题.png</html>',
    consoleMessages: ["api_key=browser-secret"],
    pageErrors: ["C:\\Users\\alice\\work\\private题.png"],
    requestFailures: ["Authorization: Bearer request-secret"],
    configuration: { dataDir: environment.dataDir, OCR_ENGINE: "mock", PRIVATE_TOKEN: "hidden" },
    browser: { name: "Microsoft Edge", version: "140.0.0.0" },
    redactionContext: {
      userHome: "C:\\Users\\alice",
      workspacePath: "C:\\Users\\alice\\work",
      tempPath: "C:\\Users\\alice\\AppData\\Local\\Temp",
      privateFilenames: ["private题.png"],
    },
  });

  expect(manifest.evidenceDir).toContain(path.join("artifacts", environment.runId, "A"));
  expect(manifest.files.map((file) => file.name)).toEqual(
    expect.arrayContaining([
      "trace.zip",
      "trace-1.zip",
      "final.png",
      "page.html",
      "console.log",
      "pageerror.log",
      "requestfailed.log",
      "fastapi.stdout.log",
      "fastapi.stderr.log",
      "app.log",
      "app.db",
      "environment.json",
      "browser.json",
    ]),
  );
  expect(manifest.files.every((file) => file.sha256.length === 64 && file.size > 0)).toBeTruthy();
  expect(Reflect.get(manifest, "missingArtifacts")).toEqual([]);
  const manifestJson = JSON.parse(await readFile(manifest.manifestPath, "utf8"));
  expect(manifestJson.files).toHaveLength(manifest.files.length);
  await expect(access(environment.scenarioRoot)).rejects.toThrow();
  for (const filename of [
    "page.html",
    "console.log",
    "pageerror.log",
    "requestfailed.log",
    "fastapi.stdout.log",
    "fastapi.stderr.log",
    "app.log",
    "environment.json",
  ]) {
    const clean = await readFile(path.join(manifest.evidenceDir, filename), "utf8");
    expect(clean).not.toMatch(/alice|private题|server-secret|private-cookie|browser-secret|request-secret|hidden/i);
  }
});

test("evidence copy failure preserves the original scenario directory", async ({}, testInfo) => {
  const environment = await createScenarioEnvironment("20260718T120001Z-run-def", "B", {
    e2eRoot: testInfo.outputPath("e2e-root"),
  });
  const occupiedEvidenceDir = path.join(
    environment.e2eRoot,
    "artifacts",
    environment.runId,
    environment.scenarioName,
  );
  await mkdir(occupiedEvidenceDir, { recursive: true });
  await writeFile(environment.databasePath, "database");

  await expect(
    collectFailureEvidence({
      environment,
      stopReport: stopped,
      tracePath: path.join(environment.scenarioRoot, "missing-trace.zip"),
      screenshotPath: path.join(environment.screenshotsDir, "missing.png"),
      html: "<html></html>",
      consoleMessages: [],
      pageErrors: [],
      requestFailures: [],
      configuration: {},
      browser: { name: "chromium", version: "140" },
      redactionContext: {},
    }),
  ).rejects.toThrow("保留原始临时目录");
  await expect(access(environment.scenarioRoot)).resolves.toBeUndefined();
});

test("browser crash records missing trace and screenshot while preserving available evidence", async ({}, testInfo) => {
  const environment = await createScenarioEnvironment("20260718T120002Z-run-crash", "C", {
    e2eRoot: testInfo.outputPath("e2e-root"),
  });
  await writeFile(environment.databasePath, "database");
  await writeFile(path.join(environment.logsDir, "fastapi.stdout.log"), "stdout");
  await writeFile(path.join(environment.logsDir, "fastapi.stderr.log"), "stderr");
  await writeFile(path.join(environment.logsDir, "app.log"), "application");

  const manifest = await collectFailureEvidence({
    environment,
    stopReport: stopped,
    tracePath: path.join(environment.scenarioRoot, "missing-trace.zip"),
    screenshotPath: path.join(environment.screenshotsDir, "missing.png"),
    html: "<html><body>fallback html</body></html>",
    consoleMessages: ["browser process exited"],
    pageErrors: ["Target page has been closed"],
    requestFailures: [],
    configuration: { OCR_ENGINE: "mock" },
    browser: { name: "Microsoft Edge", version: "150" },
    redactionContext: {},
  });

  expect(Reflect.get(manifest, "missingArtifacts")).toEqual([
    { name: "trace.zip", reason: "source-missing" },
    { name: "final.png", reason: "source-missing" },
  ]);
  expect(manifest.files.map((file) => file.name)).toEqual(
    expect.arrayContaining(["page.html", "console.log", "pageerror.log", "app.db"]),
  );
  await expect(access(environment.scenarioRoot)).rejects.toThrow();
});

test("failure evidence falls back to the verified pre-reset service logs and sqlite snapshot", async ({}, testInfo) => {
  const environment = await createScenarioEnvironment("20260718T120003Z-run-reset", "E", {
    e2eRoot: testInfo.outputPath("e2e-root"),
  });
  const preResetRoot = path.join(environment.scenarioRoot, "pre-reset-evidence");
  const archivedLogs = path.join(preResetRoot, "logs");
  const archivedDatabase = path.join(preResetRoot, "database");
  await mkdir(archivedLogs, { recursive: true });
  await mkdir(archivedDatabase, { recursive: true });
  await writeFile(path.join(archivedLogs, "fastapi.stdout.log"), "service one stdout");
  await writeFile(path.join(archivedLogs, "fastapi.stderr.log"), "service one stderr");
  await writeFile(path.join(archivedLogs, "app.log"), "service one app log");
  await writeFile(path.join(archivedDatabase, "app.db"), "service one database");
  await writeFile(path.join(archivedDatabase, "app.db-wal"), "service one wal");
  await writeFile(path.join(archivedDatabase, "app.db-shm"), "service one shm");

  const manifest = await collectFailureEvidence({
    environment,
    stopReport: stopped,
    tracePath: path.join(environment.scenarioRoot, "missing-trace.zip"),
    screenshotPath: path.join(environment.screenshotsDir, "missing.png"),
    html: "<html><body>reset failure</body></html>",
    consoleMessages: [],
    pageErrors: [],
    requestFailures: [],
    configuration: { OCR_ENGINE: "mock" },
    browser: { name: "Microsoft Edge", version: "150" },
    redactionContext: {},
  });

  expect(manifest.files.map((file) => file.name)).toEqual(
    expect.arrayContaining([
      "pre-reset-fastapi.stdout.log",
      "pre-reset-fastapi.stderr.log",
      "pre-reset-app.log",
      "pre-reset-app.db",
      "pre-reset-app.db-wal",
      "pre-reset-app.db-shm",
    ]),
  );
  await expect(access(environment.scenarioRoot)).rejects.toThrow();
});
