import { test, expect } from "@playwright/test";
import { access, mkdir } from "node:fs/promises";
import path from "node:path";

import {
  buildChildEnvironment,
  createScenarioEnvironment,
  removeScenarioDirectory,
} from "../support/environment";

test("scenario paths stay inside the current run root", async ({}, testInfo) => {
  const e2eRoot = testInfo.outputPath("isolated-e2e");
  const env = await createScenarioEnvironment("run-123", "A", { e2eRoot });

  expect(path.relative(env.runRoot, env.scenarioRoot)).not.toMatch(/^\.\./);
  expect(env.scenarioRoot.startsWith(`${env.runRoot}${path.sep}`)).toBeTruthy();
  expect(env.scenarioRoot).toContain(`${path.sep}.e2e-tmp${path.sep}run-123${path.sep}A`);
  expect(env.exportedBackupDir).toContain(
    `${path.sep}.e2e-runs${path.sep}run-123${path.sep}external${path.sep}A`,
  );
  expect(env.exportedBackupDir.startsWith(`${env.externalRunRoot}${path.sep}`)).toBeTruthy();
  expect(env.exportedBackupDir.startsWith(`${env.dataDir}${path.sep}`)).toBeFalsy();
  await expect(removeScenarioDirectory(env.runRoot, env.runRoot)).rejects.toThrow(
    "拒绝删除",
  );
  await removeScenarioDirectory(env.runRoot, env.scenarioRoot);
  await expect(removeScenarioDirectory(env.runRoot, env.scenarioRoot)).rejects.toThrow(
    "不存在",
  );
});

test("child environment excludes secrets and configures every isolated path", async ({}, testInfo) => {
  const e2eRoot = testInfo.outputPath("isolated-e2e");
  const env = await createScenarioEnvironment("run-456", "B", { e2eRoot });
  const parent = {
    SystemRoot: "C:\\Windows",
    WINDIR: "C:\\Windows",
    COMSPEC: "C:\\Windows\\System32\\cmd.exe",
    PATH: "C:\\Windows\\System32",
    PATHEXT: ".COM;.EXE;.BAT;.CMD",
    AUTHORIZATION: "Bearer private",
    COOKIE: "sid=secret",
    PRIVATE_TOKEN: "private-token",
  };

  const child = buildChildEnvironment(env, parent);

  expect(child.AUTHORIZATION).toBeUndefined();
  expect(child.COOKIE).toBeUndefined();
  expect(child.PRIVATE_TOKEN).toBeUndefined();
  expect(child.TEMP).toBe(env.tempDir);
  expect(child.TMP).toBe(env.tempDir);
  expect(child.QUESTION_PRACTICE_DATA_DIR).toBe(env.dataDir);
  expect(child.QUESTION_PRACTICE_DATABASE_URL).toBe(env.databaseUrl);
  expect(child.QUESTION_PRACTICE_MOCK_OCR_FIXTURE_PATH).toBe(env.mockOcrManifestPath);
  expect(child.QUESTION_PRACTICE_FRONTEND_DIST).toBe(env.frontendDist);
  expect(child.OCR_ENGINE).toBe("mock");
});

test("child environment fails clearly when Windows runtime keys are absent", async ({}, testInfo) => {
  const env = await createScenarioEnvironment("run-789", "C", {
    e2eRoot: testInfo.outputPath("isolated-e2e"),
  });

  expect(() =>
    buildChildEnvironment(env, {
      SystemRoot: "C:\\Windows",
      COMSPEC: "C:\\Windows\\System32\\cmd.exe",
    }),
  ).toThrow("PATH");
});

test("scenario environment refuses to reuse a stale scenario root", async ({}, testInfo) => {
  const options = { e2eRoot: testInfo.outputPath("isolated-e2e") };
  await createScenarioEnvironment("run-stale", "D", options);

  await expect(createScenarioEnvironment("run-stale", "D", options)).rejects.toThrow(
    "场景目录已存在",
  );
});

for (const failureCall of [3, 5]) {
  test(`environment creation rolls back both roots when mkdir call ${failureCall} fails`, async ({}, testInfo) => {
    const e2eRoot = testInfo.outputPath("e2e-root");
    const runId = `environment-partial-failure-${failureCall}`;
    let calls = 0;

    await expect(
      createScenarioEnvironment(runId, "A", {
        e2eRoot,
        mkdir: async (...arguments_: Parameters<typeof mkdir>) => {
          calls += 1;
          if (calls === failureCall) throw new Error("injected mkdir failure");
          return await mkdir(...arguments_);
        },
      }),
    ).rejects.toThrow("injected mkdir failure");

    await expect(access(path.join(e2eRoot, ".e2e-tmp", runId, "A"))).rejects.toThrow();
    await expect(
      access(path.join(e2eRoot, ".e2e-runs", runId, "external", "A")),
    ).rejects.toThrow();
  });
}

test("environment rollback waits for no late mkdir that can recreate deleted roots", async ({}, testInfo) => {
  const e2eRoot = testInfo.outputPath("e2e-root");
  const runId = "environment-late-mkdir";
  let calls = 0;
  const lateMkdirs: Array<Promise<unknown>> = [];

  await expect(
    createScenarioEnvironment(runId, "A", {
      e2eRoot,
      mkdir: async (...arguments_: Parameters<typeof mkdir>) => {
        calls += 1;
        if (calls === 5) throw new Error("injected concurrent mkdir failure");
        if (calls > 5) {
          const late = new Promise((resolve) => setTimeout(resolve, 25)).then(() =>
            mkdir(...arguments_),
          );
          lateMkdirs.push(late);
          return await late;
        }
        return await mkdir(...arguments_);
      },
    }),
  ).rejects.toThrow("injected concurrent mkdir failure");
  await Promise.all(lateMkdirs);

  await expect(access(path.join(e2eRoot, ".e2e-tmp", runId, "A"))).rejects.toThrow();
  await expect(
    access(path.join(e2eRoot, ".e2e-runs", runId, "external", "A")),
  ).rejects.toThrow();
});
