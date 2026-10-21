import { access, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

import { test, expect } from "@playwright/test";

import { createScenarioEnvironment } from "../support/environment";
import { ResourceCleanupError } from "../support/lifecycle-error";
import {
  acquireScenarioPrerequisites,
  cleanupSuccessfulScenario,
  clearApplicationDataPreservingExternalBackup,
  createRunId,
  prepareScenarioData,
} from "../support/scenario-fixture";

test("stopped scenario reset clears only app data and preserves fixture plus external backup", async ({}, testInfo) => {
  const environment = await createScenarioEnvironment("20260718T123456789Z-reset", "E", {
    e2eRoot: testInfo.outputPath("e2e-root"),
  });
  const fixture = environment.mockOcrManifestPath;
  const backup = path.join(environment.exportedBackupDir, "scenario-e.zip");
  await writeFile(fixture, "synthetic fixture", "utf8");
  await writeFile(backup, "synthetic backup", "utf8");
  await writeFile(environment.databasePath, "old database", "utf8");
  await writeFile(`${environment.databasePath}-wal`, "old wal", "utf8");
  await writeFile(`${environment.databasePath}-shm`, "old shm", "utf8");
  await writeFile(path.join(environment.logsDir, "fastapi.stdout.log"), "service one stdout", "utf8");
  await writeFile(path.join(environment.logsDir, "fastapi.stderr.log"), "service one stderr", "utf8");
  await writeFile(path.join(environment.logsDir, "app.log"), "service one app log", "utf8");
  await writeFile(path.join(environment.uploadsDir, "old-upload.bin"), "old upload", "utf8");
  await writeFile(path.join(environment.tempDir, "old-temp.bin"), "old temp", "utf8");

  await clearApplicationDataPreservingExternalBackup(environment, backup, true);

  await expect(access(environment.databasePath)).rejects.toThrow();
  await expect(access(path.join(environment.uploadsDir, "old-upload.bin"))).rejects.toThrow();
  await expect(access(path.join(environment.tempDir, "old-temp.bin"))).rejects.toThrow();
  await expect(readFile(fixture, "utf8")).resolves.toBe("synthetic fixture");
  await expect(readFile(backup, "utf8")).resolves.toBe("synthetic backup");
  const preResetEvidence = path.join(environment.scenarioRoot, "pre-reset-evidence");
  await expect(readFile(path.join(preResetEvidence, "database", "app.db"), "utf8")).resolves.toBe("old database");
  await expect(readFile(path.join(preResetEvidence, "database", "app.db-wal"), "utf8")).resolves.toBe("old wal");
  await expect(readFile(path.join(preResetEvidence, "database", "app.db-shm"), "utf8")).resolves.toBe("old shm");
  await expect(readFile(path.join(preResetEvidence, "logs", "fastapi.stdout.log"), "utf8")).resolves.toBe("service one stdout");
  await expect(readFile(path.join(preResetEvidence, "logs", "fastapi.stderr.log"), "utf8")).resolves.toBe("service one stderr");
  await expect(readFile(path.join(preResetEvidence, "logs", "app.log"), "utf8")).resolves.toBe("service one app log");
  await expect(access(environment.dataDir)).resolves.toBeUndefined();
  await expect(access(environment.uploadsDir)).resolves.toBeUndefined();
  await expect(access(environment.tempDir)).resolves.toBeUndefined();
});

test("scenario reset refuses a running service or a backup outside the controlled export directory", async ({}, testInfo) => {
  const environment = await createScenarioEnvironment("20260718T123456789Z-reset-guard", "E", {
    e2eRoot: testInfo.outputPath("e2e-root"),
  });
  const backup = path.join(environment.exportedBackupDir, "scenario-e.zip");
  const outside = path.join(environment.externalRunRoot, "outside.zip");
  await writeFile(backup, "synthetic backup", "utf8");
  await writeFile(outside, "outside backup", "utf8");

  await expect(
    clearApplicationDataPreservingExternalBackup(environment, backup, false),
  ).rejects.toThrow("服务和 worker 全部停止");
  await expect(
    clearApplicationDataPreservingExternalBackup(environment, outside, true),
  ).rejects.toThrow("外置备份目录");
  await expect(readFile(backup, "utf8")).resolves.toBe("synthetic backup");
  await expect(readFile(outside, "utf8")).resolves.toBe("outside backup");
});

test("scenario fixture creates a timestamped unique controlled run id", () => {
  const runId = createRunId(new Date("2026-07-18T12:34:56.789Z"), "a1b2c3d4");

  expect(runId).toBe("20260718T123456789Z-a1b2c3d4");
  expect(runId).not.toMatch(/[\\/:]/);
});

test("scenario fixture invokes the synthetic generator without creating business data", async ({}, testInfo) => {
  const environment = await createScenarioEnvironment("20260718T123456789Z-fixture", "D", {
    e2eRoot: testInfo.outputPath("e2e-root"),
  });

  const data = await prepareScenarioData(environment);

  expect(data.scenario).toBe("D");
  expect(data.importDirectories).toHaveLength(2);
  expect(data.images).toHaveLength(2);
  expect(path.resolve(data.manifest)).toBe(environment.mockOcrManifestPath);
  await expect(access(environment.databasePath)).rejects.toThrow();
});

test("successful scenario cleanup removes app and exported backup directories precisely", async ({}, testInfo) => {
  const environment = await createScenarioEnvironment("20260718T123456789Z-clean", "E", {
    e2eRoot: testInfo.outputPath("e2e-root"),
  });

  await cleanupSuccessfulScenario(environment);

  await expect(access(environment.scenarioRoot)).rejects.toThrow();
  await expect(access(environment.exportedBackupDir)).rejects.toThrow();
  await expect(access(environment.runRoot)).rejects.toThrow();
  await expect(access(environment.externalRunRoot)).rejects.toThrow();
});

test("successful cleanup retains a shared run root while a sibling scenario exists", async ({}, testInfo) => {
  const e2eRoot = testInfo.outputPath("e2e-root");
  const first = await createScenarioEnvironment("20260718T123456789Z-shared", "A", { e2eRoot });
  const sibling = await createScenarioEnvironment("20260718T123456789Z-shared", "B", { e2eRoot });

  await cleanupSuccessfulScenario(first);

  await expect(access(first.scenarioRoot)).rejects.toThrow();
  await expect(access(first.exportedBackupDir)).rejects.toThrow();
  await expect(access(sibling.scenarioRoot)).resolves.toBeUndefined();
  await expect(access(sibling.exportedBackupDir)).resolves.toBeUndefined();
  await expect(access(first.runRoot)).resolves.toBeUndefined();
  await expect(access(first.externalRunRoot)).resolves.toBeUndefined();
});

test("pre-server setup failure removes both controlled scenario directories", async ({}, testInfo) => {
  const e2eRoot = testInfo.outputPath("e2e-root");
  let scenarioRoot = "";
  let exportedBackupDir = "";

  await expect(
    acquireScenarioPrerequisites(
      "20260718T123456789Z-setupfail",
      "A",
      {},
      {
        createEnvironment: async (runId, scenarioName) => {
          const environment = await createScenarioEnvironment(runId, scenarioName, { e2eRoot });
          scenarioRoot = environment.scenarioRoot;
          exportedBackupDir = environment.exportedBackupDir;
          return environment;
        },
        prepareData: async () => {
          throw new Error("injected fixture generation failure");
        },
      },
    ),
  ).rejects.toThrow("injected fixture generation failure");

  await expect(access(scenarioRoot)).rejects.toThrow();
  await expect(access(exportedBackupDir)).rejects.toThrow();
});

for (const resource of ["server", "browser"] as const) {
  test(`${resource} cleanup uncertainty preserves both controlled scenario directories`, async ({}, testInfo) => {
    const e2eRoot = testInfo.outputPath("e2e-root");
    let scenarioRoot = "";
    let exportedBackupDir = "";
    const fakeServer = { marker: "server" } as never;

    await expect(
      acquireScenarioPrerequisites(
        `20260718T123456789Z-${resource}`,
        "A",
        {} as never,
        {
          createEnvironment: async (runId, scenarioName) => {
            const environment = await createScenarioEnvironment(runId, scenarioName, { e2eRoot });
            scenarioRoot = environment.scenarioRoot;
            exportedBackupDir = environment.exportedBackupDir;
            return environment;
          },
          prepareData: async () => ({}) as never,
          start: async () => {
            if (resource === "server") {
              throw new ResourceCleanupError("server", false, [31001], "injected server residue");
            }
            return fakeServer;
          },
          acquireBrowser: async () => {
            throw new ResourceCleanupError("browser", false, [42001], "injected browser residue");
          },
          stop: async () => ({
            graceful: true,
            forcedPids: [],
            errors: [],
            mainExited: true,
            childrenExited: true,
            healthUnreachable: true,
            portReleased: true,
            remainingPids: [],
          }),
        },
      ),
    ).rejects.toThrow(resource === "server" ? "31001" : "42001");

    await expect(access(scenarioRoot)).resolves.toBeUndefined();
    await expect(access(exportedBackupDir)).resolves.toBeUndefined();
  });
}
