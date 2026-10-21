import { spawn, type ChildProcess } from "node:child_process";
import { closeSync, existsSync, openSync } from "node:fs";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import net from "node:net";
import path from "node:path";

import { buildChildEnvironment, type ScenarioEnvironment } from "./environment";
import { ResourceCleanupError } from "./lifecycle-error";

export interface ServerHandle {
  environment: ScenarioEnvironment;
  rootPid: number;
  rootStartedAt: number;
  serverPid: number;
  serverStartedAt: number;
  processTree: Array<{ pid: number; startedAt: number }>;
  host: "127.0.0.1";
  port: number;
  baseUrl: string;
  stdoutPath: string;
  stderrPath: string;
  shutdownPath: string;
}

export interface StopReport {
  graceful: boolean;
  forcedPids: number[];
  errors: string[];
  mainExited: boolean;
  childrenExited: boolean;
  healthUnreachable: boolean;
  portReleased: boolean;
  remainingPids: number[];
}

export interface StartServerOptions {
  afterHealth?: (handle: ServerHandle) => void | Promise<void>;
  processIdentity?: (pid: number) => Promise<{ pid: number; started_at: number }>;
}

export interface StopServerOptions {
  writeShutdown?: () => Promise<void>;
  terminateCommand?: () => Promise<CommandResult>;
}

interface CommandResult {
  code: number;
  stdout: string;
  stderr: string;
}

const liveProcesses = new Map<number, { child: ChildProcess; closed: Promise<number | null> }>();

export function pythonExecutable(environment: ScenarioEnvironment): string {
  const configured = process.env.E2E_PYTHON;
  const candidate = configured || path.join(environment.repoRoot, "backend", ".venv", "Scripts", "python.exe");
  if (!existsSync(candidate)) {
    throw new Error(`未找到 E2E Python：${candidate}；请先运行 install.bat 或设置 E2E_PYTHON`);
  }
  return candidate;
}

async function runCommand(
  executable: string,
  arguments_: string[],
  options: { cwd: string; env: NodeJS.ProcessEnv; timeoutMs?: number },
): Promise<CommandResult> {
  return await new Promise((resolve, reject) => {
    const child = spawn(executable, arguments_, {
      cwd: options.cwd,
      env: options.env,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout?.setEncoding("utf8");
    child.stderr?.setEncoding("utf8");
    child.stdout?.on("data", (chunk) => (stdout += chunk));
    child.stderr?.on("data", (chunk) => (stderr += chunk));
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error(`命令超时：${arguments_.join(" ")}`));
    }, options.timeoutMs ?? 30_000);
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code: code ?? -1, stdout, stderr });
    });
  });
}

async function waitUntil<T>(
  probe: () => Promise<T | undefined>,
  description: string,
  timeoutMs = 15_000,
): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  let lastError: unknown;
  while (Date.now() < deadline) {
    try {
      const result = await probe();
      if (result !== undefined) return result;
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error(`等待${description}超时${lastError ? `：${String(lastError)}` : ""}`);
}

async function processIdentity(
  python: string,
  backendRoot: string,
  childEnvironment: NodeJS.ProcessEnv,
  pid: number,
): Promise<{ pid: number; started_at: number }> {
  const result = await runCommand(
    python,
    ["-m", "app.testing.process_control", "identity", "--pid", String(pid)],
    { cwd: backendRoot, env: childEnvironment },
  );
  if (result.code !== 0) {
    throw new Error(`无法记录进程身份：${result.stderr || result.stdout}`);
  }
  return JSON.parse(result.stdout) as { pid: number; started_at: number };
}

export async function startServer(
  environment: ScenarioEnvironment,
  options: StartServerOptions = {},
): Promise<ServerHandle> {
  const python = pythonExecutable(environment);
  const backendRoot = path.join(environment.repoRoot, "backend");
  const childEnvironment = buildChildEnvironment(environment);
  if (!existsSync(path.join(environment.frontendDist, "index.html"))) {
    throw new Error("前端生产构建不存在，拒绝启动 E2E 服务");
  }
  if (!existsSync(environment.mockOcrManifestPath)) {
    throw new Error("Mock OCR 场景清单不存在");
  }
  await rm(environment.controlFile, { force: true });
  await mkdir(environment.logsDir, { recursive: true });
  const migration = await runCommand(
    python,
    ["-m", "alembic", "-c", path.join(backendRoot, "alembic.ini"), "upgrade", "head"],
    { cwd: backendRoot, env: childEnvironment, timeoutMs: 30_000 },
  );
  if (migration.code !== 0) {
    throw new Error(`数据库迁移失败：${migration.stderr || migration.stdout}`);
  }

  const stdoutPath = path.join(environment.logsDir, "fastapi.stdout.log");
  const stderrPath = path.join(environment.logsDir, "fastapi.stderr.log");
  const stdoutDescriptor = openSync(stdoutPath, "a");
  const stderrDescriptor = openSync(stderrPath, "a");
  const child = spawn(
    python,
    ["-m", "app.testing.e2e_server", "--control", environment.controlFile],
    {
      cwd: backendRoot,
      env: childEnvironment,
      windowsHide: true,
      stdio: ["ignore", stdoutDescriptor, stderrDescriptor],
    },
  );
  closeSync(stdoutDescriptor);
  closeSync(stderrDescriptor);
  if (!child.pid) throw new Error("FastAPI 进程未返回 PID");
  const closed = new Promise<number | null>((resolve) => child.once("close", resolve));
  liveProcesses.set(child.pid, { child, closed });
  let rootIdentity: { pid: number; started_at: number } | undefined;
  let control:
    | {
        pid: number;
        started_at: number;
        host: string;
        port: number;
        shutdown_path: string;
      }
    | undefined;
  let processTree: Array<{ pid: number; startedAt: number }> = [];
  try {
    rootIdentity = await (options.processIdentity
      ? options.processIdentity(child.pid)
      : processIdentity(python, backendRoot, childEnvironment, child.pid));
    processTree = [{ pid: rootIdentity.pid, startedAt: rootIdentity.started_at }];
    control = await waitUntil(async () => {
    if (child.exitCode !== null) {
      const stderr = await readFile(stderrPath, "utf8").catch(() => "");
      throw new Error(`FastAPI 过早退出（${child.exitCode}）：${stderr}`);
    }
    if (!existsSync(environment.controlFile)) return undefined;
      return JSON.parse(await readFile(environment.controlFile, "utf8")) as {
        pid: number;
        started_at: number;
        host: string;
        port: number;
        shutdown_path: string;
      };
    }, "Uvicorn 原子 socket 控制文件");
    if (control.host !== "127.0.0.1" || control.port <= 0) {
      throw new Error("Uvicorn 返回了无效监听地址");
    }
    const baseUrl = `http://${control.host}:${control.port}`;
    await waitUntil(async () => {
      const response = await fetch(`${baseUrl}/api/health`, {
        signal: AbortSignal.timeout(1_000),
      });
      return response.status === 200 ? true : undefined;
    }, "FastAPI 健康检查");
    const listener = await runCommand(
    python,
    [
      "-m",
      "app.testing.process_control",
      "listener",
      "--pid",
      String(control.pid),
      "--started-at",
      String(control.started_at),
      "--host",
      control.host,
      "--port",
      String(control.port),
    ],
    { cwd: backendRoot, env: childEnvironment },
    );
    if (listener.code !== 0 || JSON.parse(listener.stdout).listening !== true) {
      throw new Error(`监听端口不属于已记录服务进程：${listener.stderr || listener.stdout}`);
    }
    const inspected = await runCommand(
    python,
    [
      "-m",
      "app.testing.process_control",
      "inspect",
      "--pid",
      String(rootIdentity.pid),
      "--started-at",
      String(rootIdentity.started_at),
    ],
    { cwd: backendRoot, env: childEnvironment },
    );
    if (inspected.code !== 0) {
      throw new Error(`无法保存完整进程树：${inspected.stderr || inspected.stdout}`);
    }
    const treePayload = JSON.parse(inspected.stdout) as {
      root: { pid: number; started_at: number };
      children: Array<{ pid: number; started_at: number }>;
    };
    processTree = [treePayload.root, ...treePayload.children].map((identity) => ({
      pid: identity.pid,
      startedAt: identity.started_at,
    }));
    if (!processTree.some((identity) => identity.pid === control?.pid)) {
      throw new Error("Uvicorn 进程不属于已记录的根进程树");
    }
    const handle: ServerHandle = {
      environment,
      rootPid: rootIdentity.pid,
      rootStartedAt: rootIdentity.started_at,
      serverPid: control.pid,
      serverStartedAt: control.started_at,
      processTree,
      host: "127.0.0.1",
      port: control.port,
      baseUrl,
      stdoutPath,
      stderrPath,
      shutdownPath: control.shutdown_path,
    };
    await options.afterHealth?.(handle);
    return handle;
  } catch (error) {
    let cleanupFailure: unknown;
    if (!rootIdentity || !control) {
      const recoveredControl = await waitUntil(async () => {
        if (!existsSync(environment.controlFile)) return undefined;
        return JSON.parse(await readFile(environment.controlFile, "utf8")) as {
          pid: number;
          started_at: number;
          host: string;
          port: number;
          shutdown_path: string;
        };
      }, "失败启动的控制文件", 5_000).catch(() => undefined);
      if (recoveredControl?.pid === child.pid) {
        control ??= recoveredControl;
        rootIdentity ??= {
          pid: recoveredControl.pid,
          started_at: recoveredControl.started_at,
        };
        processTree = [
          { pid: recoveredControl.pid, startedAt: recoveredControl.started_at },
        ];
      }
    }
    if (control && rootIdentity) {
      const provisional: ServerHandle = {
        environment,
        rootPid: rootIdentity.pid,
        rootStartedAt: rootIdentity.started_at,
        serverPid: control.pid,
        serverStartedAt: control.started_at,
        processTree: processTree.some((identity) => identity.pid === control?.pid)
          ? processTree
          : [
              ...processTree,
              { pid: control.pid, startedAt: control.started_at },
            ],
        host: "127.0.0.1",
        port: control.port,
        baseUrl: `http://127.0.0.1:${control.port}`,
        stdoutPath,
        stderrPath,
        shutdownPath: control.shutdown_path,
      };
      const report = await stopServer(provisional).catch((stopError) => {
        cleanupFailure = stopError;
        return undefined;
      });
      if (report && report.remainingPids.length > 0) cleanupFailure = report;
    } else {
      cleanupFailure = await terminateSpawnedChild(child, child.pid, closed).catch(
        (stopError) => stopError,
      );
    }
    if (cleanupFailure) {
      const remainingPids =
        typeof cleanupFailure === "object" &&
        cleanupFailure !== null &&
        Array.isArray(Reflect.get(cleanupFailure, "remainingPids"))
          ? (Reflect.get(cleanupFailure, "remainingPids") as number[])
          : [child.pid];
      throw new ResourceCleanupError(
        "server",
        false,
        remainingPids,
        `服务启动失败且清理未完成（残留 PID：${remainingPids.join(",") || "未知"}）：${String(cleanupFailure)}`,
        error,
      );
    }
    throw error;
  }
}

async function terminateSpawnedChild(
  child: ChildProcess,
  pid: number,
  closed: Promise<number | null>,
): Promise<void> {
  if (child.exitCode === null && !child.killed) child.kill();
  await Promise.race([
    closed,
    new Promise((_, reject) =>
      setTimeout(() => reject(new Error(`PID ${pid} 未在限定时间内退出`)), 5_000),
    ),
  ]);
  liveProcesses.delete(pid);
}

async function forceTerminateRoot(
  python: string,
  backendRoot: string,
  childEnvironment: NodeJS.ProcessEnv,
  pid: number,
  startedAt: number,
): Promise<void> {
  const result = await runCommand(
    python,
    [
      "-m",
      "app.testing.process_control",
      "terminate",
      "--pid",
      String(pid),
      "--started-at",
      String(startedAt),
    ],
    { cwd: backendRoot, env: childEnvironment, timeoutMs: 20_000 },
  );
  const report = result.stdout.trim()
    ? (JSON.parse(result.stdout) as { remaining?: number[] })
    : { remaining: [pid] };
  if (result.code !== 0 || report.remaining?.length) {
    throw new Error(result.stderr || result.stdout || `PID ${pid} 仍未退出`);
  }
  const tracked = liveProcesses.get(pid);
  if (tracked) {
    await Promise.race([
      tracked.closed,
      new Promise((resolve) => setTimeout(resolve, 5_000)),
    ]);
    liveProcesses.delete(pid);
  }
}

export interface TrackedProcessIdentity {
  pid: number;
  startedAt: number;
  parentPid: number;
}

export async function snapshotDescendantProcesses(
  environment: ScenarioEnvironment,
  rootPid = process.pid,
): Promise<TrackedProcessIdentity[]> {
  const python = pythonExecutable(environment);
  const backendRoot = path.join(environment.repoRoot, "backend");
  const childEnvironment = buildChildEnvironment(environment);
  const root = await processIdentity(python, backendRoot, childEnvironment, rootPid);
  const inspected = await runCommand(
    python,
    [
      "-m",
      "app.testing.process_control",
      "inspect",
      "--pid",
      String(root.pid),
      "--started-at",
      String(root.started_at),
    ],
    { cwd: backendRoot, env: childEnvironment },
  );
  if (inspected.code !== 0) {
    throw new Error(`无法记录宿主进程子树：${inspected.stderr || inspected.stdout}`);
  }
  const payload = JSON.parse(inspected.stdout) as {
    root: { pid: number; started_at: number; parent_pid: number };
    children: Array<{ pid: number; started_at: number; parent_pid: number }>;
  };
  return payload.children.map((identity) => ({
    pid: identity.pid,
    startedAt: identity.started_at,
    parentPid: identity.parent_pid,
  }));
}

export async function recordedIdentityExists(
  environment: ScenarioEnvironment,
  identity: Pick<TrackedProcessIdentity, "pid" | "startedAt">,
): Promise<boolean> {
  const python = pythonExecutable(environment);
  return await identityExists(
    python,
    path.join(environment.repoRoot, "backend"),
    buildChildEnvironment(environment),
    identity.pid,
    identity.startedAt,
  );
}

export async function terminateRecordedIdentity(
  environment: ScenarioEnvironment,
  identity: Pick<TrackedProcessIdentity, "pid" | "startedAt">,
): Promise<void> {
  await forceTerminateRoot(
    pythonExecutable(environment),
    path.join(environment.repoRoot, "backend"),
    buildChildEnvironment(environment),
    identity.pid,
    identity.startedAt,
  );
}

async function identityExists(
  python: string,
  backendRoot: string,
  childEnvironment: NodeJS.ProcessEnv,
  pid: number,
  startedAt: number,
): Promise<boolean> {
  const result = await runCommand(
    python,
    ["-m", "app.testing.process_control", "identity", "--pid", String(pid)],
    { cwd: backendRoot, env: childEnvironment },
  );
  if (result.code !== 0) return false;
  const current = JSON.parse(result.stdout) as { started_at: number };
  return Math.abs(current.started_at - startedAt) <= 0.01;
}

async function portCanBind(host: string, port: number): Promise<boolean> {
  return await new Promise((resolve) => {
    const server = net.createServer();
    server.once("error", () => resolve(false));
    server.listen({ host, port, exclusive: true }, () => {
      server.close(() => resolve(true));
    });
  });
}

export async function stopServer(
  handle: ServerHandle,
  options: StopServerOptions = {},
): Promise<StopReport> {
  const python = pythonExecutable(handle.environment);
  const backendRoot = path.join(handle.environment.repoRoot, "backend");
  const childEnvironment = buildChildEnvironment(handle.environment);
  const stopErrors: string[] = [];
  let graceful = false;
  let forcedPids: number[] = [];
  let remainingPids: number[] = [];
  try {
    await (options.writeShutdown
      ? options.writeShutdown()
      : writeFile(handle.shutdownPath, "shutdown\n", { encoding: "utf8", flag: "wx" }));
    graceful = await waitUntil(async () => {
      const states = await Promise.all(
        handle.processTree.map((identity) =>
          identityExists(
            python,
            backendRoot,
            childEnvironment,
            identity.pid,
            identity.startedAt,
          ),
        ),
      );
      return states.every((exists) => !exists) ? true : undefined;
    }, "FastAPI lifespan 优雅退出", 10_000).catch(() => false);
  } catch (error) {
    stopErrors.push(`优雅退出触发失败：${String(error)}`);
  }
  let fallbackNeeded = false;
  if (!graceful) {
    try {
      const termination = await (options.terminateCommand
        ? options.terminateCommand()
        : runCommand(
            python,
            [
              "-m",
              "app.testing.process_control",
              "terminate",
              "--pid",
              String(handle.rootPid),
              "--started-at",
              String(handle.rootStartedAt),
            ],
            { cwd: backendRoot, env: childEnvironment, timeoutMs: 20_000 },
          ));
      if (termination.stdout.trim()) {
        const report = JSON.parse(termination.stdout) as {
          remaining?: number[];
          terminated?: number[];
          killed?: number[];
        };
        remainingPids = report.remaining ?? [];
        forcedPids = [...new Set([...(report.terminated ?? []), ...(report.killed ?? [])])];
      }
      if (termination.code !== 0) {
        stopErrors.push(`精确进程树兜底失败：${termination.stderr || termination.stdout}`);
        fallbackNeeded = true;
      }
    } catch (error) {
      stopErrors.push(`精确进程树兜底异常：${String(error)}`);
      fallbackNeeded = true;
    }
    if (fallbackNeeded) {
      try {
        await forceTerminateRoot(
          python,
          backendRoot,
          childEnvironment,
          handle.rootPid,
          handle.rootStartedAt,
        );
        forcedPids = handle.processTree.map((identity) => identity.pid);
        remainingPids = [];
      } catch (error) {
        stopErrors.push(`记录身份二次兜底失败：${String(error)}`);
      }
    }
  }
  const tracked = liveProcesses.get(handle.rootPid);
  if (tracked) {
    await Promise.race([
      tracked.closed,
      new Promise((resolve) => setTimeout(resolve, 5_000)),
    ]);
    liveProcesses.delete(handle.rootPid);
  }
  const mainExited = await identityExists(
    python,
    backendRoot,
    childEnvironment,
    handle.rootPid,
    handle.rootStartedAt,
  ).then((exists) => !exists, (error) => {
    stopErrors.push(`主进程身份复核失败：${String(error)}`);
    return false;
  });
  const childStates = await Promise.all(
    handle.processTree
      .filter((identity) => identity.pid !== handle.rootPid)
      .map((identity) =>
        identityExists(
          python,
          backendRoot,
          childEnvironment,
          identity.pid,
          identity.startedAt,
        ).catch((error) => {
          stopErrors.push(`子进程身份复核失败：${String(error)}`);
          return true;
        }),
      ),
  );
  const childrenExited = childStates.every((exists) => !exists);
  const healthUnreachable = await waitUntil(async () => {
    try {
      await fetch(`${handle.baseUrl}/api/health`, { signal: AbortSignal.timeout(300) });
      return undefined;
    } catch {
      return true;
    }
  }, "健康检查停止", 10_000).catch(() => false);
  const portReleased = await waitUntil(
    async () => ((await portCanBind(handle.host, handle.port)) ? true : undefined),
    "监听端口释放",
    10_000,
  ).catch(() => false);
  if (!mainExited) remainingPids.push(handle.rootPid);
  remainingPids.push(
    ...handle.processTree
      .filter((identity) => identity.pid !== handle.rootPid)
      .filter((_, index) => childStates[index])
      .map((identity) => identity.pid),
  );
  remainingPids = [...new Set(remainingPids)];
  return {
    graceful,
    forcedPids,
    errors: stopErrors,
    mainExited,
    childrenExited,
    healthUnreachable,
    portReleased,
    remainingPids,
  };
}
