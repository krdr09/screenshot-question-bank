import { existsSync } from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { createRunId } from "./scenario-fixture";

export type BuildRunner = (repoRoot: string) => void | Promise<void>;

export async function buildFrontendProduction(
  repoRoot: string,
  runner: BuildRunner = runPnpmBuild,
): Promise<void> {
  await runner(repoRoot);
  const indexPath = path.join(repoRoot, "frontend", "dist", "index.html");
  if (!existsSync(indexPath)) {
    throw new Error("前端生产构建未生成 frontend/dist/index.html");
  }
}

export default async function globalSetup(): Promise<void> {
  const repoRoot = path.resolve(fileURLToPath(new URL("../..", import.meta.url)));
  await buildFrontendProduction(repoRoot);
  process.env.E2E_RUN_ID ||= createRunId();
}

function runPnpmBuild(repoRoot: string): void {
  const pnpmCli = process.env.npm_execpath;
  if (!pnpmCli || !existsSync(pnpmCli)) {
    throw new Error("无法定位 pnpm CLI；请通过 pnpm 运行阶段 10 E2E 命令");
  }
  const result = spawnSync(
    process.execPath,
    [pnpmCli, "--dir", path.join(repoRoot, "frontend"), "run", "build"],
    { cwd: repoRoot, encoding: "utf8", windowsHide: true },
  );
  if (result.error || result.status !== 0) {
    throw new Error(
      `前端生产构建失败（exit ${result.status ?? "spawn-error"}）：${result.stderr || result.stdout || result.error}`,
    );
  }
}
