import { mkdir, writeFile } from "node:fs/promises";
import { readFile } from "node:fs/promises";
import net from "node:net";
import path from "node:path";

import { test, expect } from "@playwright/test";

import { createScenarioEnvironment } from "../support/environment";
import { startServer, stopServer, type ServerHandle } from "../support/server";

test("process tree starts the real service and releases health and port on stop", async ({}, testInfo) => {
  const e2eRoot = testInfo.outputPath("isolated-e2e");
  const frontendDist = path.join(e2eRoot, "dist");
  await mkdir(frontendDist, { recursive: true });
  await writeFile(path.join(frontendDist, "index.html"), "<!doctype html><title>E2E</title>", "utf8");
  const environment = await createScenarioEnvironment("process-run", "A", {
    e2eRoot,
    frontendDist,
  });
  await writeFile(
    environment.mockOcrManifestPath,
    JSON.stringify({ schema_version: 1, results_by_sha256: {} }),
    "utf8",
  );
  let handle: ServerHandle | undefined;
  let stopped = false;
  try {
    handle = await startServer(environment);
    const response = await fetch(`${handle.baseUrl}/api/health`);

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toMatchObject({ status: "ok", host: "127.0.0.1" });
    expect(handle.rootPid).toBeGreaterThan(0);
    expect(handle.serverPid).toBeGreaterThan(0);
    expect(Reflect.get(handle, "processTree")).toEqual(
      expect.arrayContaining([
        { pid: handle.rootPid, startedAt: handle.rootStartedAt },
        { pid: handle.serverPid, startedAt: handle.serverStartedAt },
      ]),
    );

    const report = await stopServer(handle);
    stopped = true;

    expect(report).toMatchObject({
      mainExited: true,
      childrenExited: true,
      healthUnreachable: true,
      portReleased: true,
      remainingPids: [],
    });
    expect(Reflect.get(report, "graceful")).toBe(true);
    expect(Reflect.get(report, "forcedPids")).toEqual([]);
  } finally {
    if (handle && !stopped) {
      await stopServer(handle);
    }
  }
});

test("post-spawn startup failure performs exact cleanup before rejecting", async ({}, testInfo) => {
  const e2eRoot = testInfo.outputPath("isolated-e2e");
  const frontendDist = path.join(e2eRoot, "dist");
  await mkdir(frontendDist, { recursive: true });
  await writeFile(path.join(frontendDist, "index.html"), "<!doctype html><title>E2E</title>", "utf8");
  const environment = await createScenarioEnvironment("startup-failure", "B", {
    e2eRoot,
    frontendDist,
  });
  await writeFile(
    environment.mockOcrManifestPath,
    JSON.stringify({ schema_version: 1, results_by_sha256: {} }),
    "utf8",
  );

  const outcome = await (startServer as unknown as Function)(environment, {
    afterHealth: () => {
      throw new Error("injected post-spawn failure");
    },
  }).then(
    (handle: ServerHandle) => ({ handle }),
    (error: unknown) => ({ error }),
  );
  if ("handle" in outcome) await stopServer(outcome.handle);

  expect(Reflect.get(outcome, "error")).toBeInstanceOf(Error);
  expect(String(Reflect.get(outcome, "error"))).toContain("injected post-spawn failure");
  const control = JSON.parse(await readFile(environment.controlFile, "utf8")) as {
    port: number;
  };
  await expect(fetch(`http://127.0.0.1:${control.port}/api/health`)).rejects.toThrow();
  await expect(
    new Promise<void>((resolve, reject) => {
      const probe = net.createServer();
      probe.once("error", reject);
      probe.listen({ host: "127.0.0.1", port: control.port, exclusive: true }, () => {
        probe.close(() => resolve());
      });
    }),
  ).resolves.toBeUndefined();
});

test("process identity acquisition failure still closes the spawned server", async ({}, testInfo) => {
  const e2eRoot = testInfo.outputPath("isolated-e2e");
  const frontendDist = path.join(e2eRoot, "dist");
  await mkdir(frontendDist, { recursive: true });
  await writeFile(path.join(frontendDist, "index.html"), "<!doctype html><title>E2E</title>", "utf8");
  const environment = await createScenarioEnvironment("identity-failure", "B", {
    e2eRoot,
    frontendDist,
  });
  await writeFile(
    environment.mockOcrManifestPath,
    JSON.stringify({ schema_version: 1, results_by_sha256: {} }),
    "utf8",
  );
  let spawnedPid = 0;

  await expect(
    (startServer as unknown as Function)(environment, {
      processIdentity: async (pid: number) => {
        spawnedPid = pid;
        throw new Error("injected identity acquisition failure");
      },
    }),
  ).rejects.toThrow("injected identity acquisition failure");

  expect(spawnedPid).toBeGreaterThan(0);
  await expect
    .poll(() => {
      try {
        process.kill(spawnedPid, 0);
        return true;
      } catch {
        return false;
      }
    })
    .toBe(false);
});

test("browser acquisition failure stops the already started service", async ({ playwright }, testInfo) => {
  const e2eRoot = testInfo.outputPath("isolated-e2e");
  const frontendDist = path.join(e2eRoot, "dist");
  await mkdir(frontendDist, { recursive: true });
  await writeFile(path.join(frontendDist, "index.html"), "<!doctype html><title>E2E</title>", "utf8");
  const environment = await createScenarioEnvironment("browser-failure", "C", {
    e2eRoot,
    frontendDist,
  });
  await writeFile(
    environment.mockOcrManifestPath,
    JSON.stringify({ schema_version: 1, results_by_sha256: {} }),
    "utf8",
  );
  const handle = await startServer(environment);
  const fixtureModule = await import("../support/scenario-fixture");
  const acquire = Reflect.get(fixtureModule, "acquireBrowserRuntime");

  if (typeof acquire !== "function") await stopServer(handle);
  expect(typeof acquire).toBe("function");
  await expect(
    acquire(handle, playwright, async () => {
      throw new Error("injected Edge launch failure");
    }),
  ).rejects.toThrow("injected Edge launch failure");
  await expect(fetch(`${handle.baseUrl}/api/health`)).rejects.toThrow();
});

test("browser acquisition reports cleanup errors and remaining exact identities", async ({ playwright }, testInfo) => {
  const e2eRoot = testInfo.outputPath("isolated-e2e");
  const frontendDist = path.join(e2eRoot, "dist");
  await mkdir(frontendDist, { recursive: true });
  await writeFile(path.join(frontendDist, "index.html"), "<!doctype html><title>E2E</title>", "utf8");
  const environment = await createScenarioEnvironment("browser-cleanup-failure", "C", {
    e2eRoot,
    frontendDist,
  });
  await writeFile(
    environment.mockOcrManifestPath,
    JSON.stringify({ schema_version: 1, results_by_sha256: {} }),
    "utf8",
  );
  const handle = await startServer(environment);
  const fixtureModule = await import("../support/scenario-fixture");
  const tracked = { pid: 424242, startedAt: 1234.5, parentPid: process.pid };
  let snapshotCall = 0;

  await expect(
    fixtureModule.acquireBrowserRuntime(
      handle,
      playwright,
      async () =>
        ({
          newContext: async () => ({
            newPage: async () => {
              throw new Error("injected context setup failure");
            },
            close: async () => undefined,
          }),
          close: async () => undefined,
        }) as never,
      {
        snapshot: async () => (snapshotCall++ === 0 ? [] : [tracked]),
        cleanup: async () => {
          throw new Error("injected browser cleanup failure");
        },
        existing: async () => [tracked],
      },
    ),
  ).rejects.toThrow(/browser cleanup failure.*424242|424242.*browser cleanup failure/i);
  await expect(fetch(`${handle.baseUrl}/api/health`)).rejects.toThrow();
});

test("stop helper failure still falls back to recorded process identities", async ({}, testInfo) => {
  const e2eRoot = testInfo.outputPath("isolated-e2e");
  const frontendDist = path.join(e2eRoot, "dist");
  await mkdir(frontendDist, { recursive: true });
  await writeFile(path.join(frontendDist, "index.html"), "<!doctype html><title>E2E</title>", "utf8");
  const environment = await createScenarioEnvironment("stop-helper-failure", "D", {
    e2eRoot,
    frontendDist,
  });
  await writeFile(
    environment.mockOcrManifestPath,
    JSON.stringify({ schema_version: 1, results_by_sha256: {} }),
    "utf8",
  );
  const handle = await startServer(environment);
  let gracefulAttempted = false;
  let helperAttempted = false;

  const report = await (stopServer as unknown as Function)(handle, {
    writeShutdown: async () => {
      gracefulAttempted = true;
      throw new Error("injected graceful trigger failure");
    },
    terminateCommand: async () => {
      helperAttempted = true;
      throw new Error("injected termination helper failure");
    },
  });

  expect(gracefulAttempted).toBe(true);
  expect(helperAttempted).toBe(true);
  expect(report.remainingPids).toEqual([]);
  expect(report.forcedPids).toEqual(expect.arrayContaining([handle.rootPid, handle.serverPid]));
  expect(report.errors.join("\n")).toContain("injected termination helper failure");
  await expect(fetch(`${handle.baseUrl}/api/health`)).rejects.toThrow();
});

test("real Edge descendants are recorded and verified gone after browser close", async ({ playwright }, testInfo) => {
  const e2eRoot = testInfo.outputPath("isolated-e2e");
  const frontendDist = path.join(e2eRoot, "dist");
  await mkdir(frontendDist, { recursive: true });
  await writeFile(path.join(frontendDist, "index.html"), "<!doctype html><title>E2E</title>", "utf8");
  const environment = await createScenarioEnvironment("edge-process-tree", "E", {
    e2eRoot,
    frontendDist,
  });
  await writeFile(
    environment.mockOcrManifestPath,
    JSON.stringify({ schema_version: 1, results_by_sha256: {} }),
    "utf8",
  );
  const handle = await startServer(environment);
  const fixtureModule = await import("../support/scenario-fixture");
  const runtime = await fixtureModule.acquireBrowserRuntime(handle, playwright);
  const tracked = Reflect.get(runtime, "browserProcesses");
  let browserStop: { forcedPids: number[]; remainingPids: number[] } | undefined;
  try {
    expect(Array.isArray(tracked) && tracked.length > 0).toBeTruthy();
  } finally {
    browserStop = await fixtureModule.closeBrowserRuntime(runtime, environment);
    await stopServer(handle);
  }
  expect(browserStop).toEqual({ forcedPids: [], remainingPids: [] });
});
