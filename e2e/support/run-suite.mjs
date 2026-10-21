import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { runAcceptanceSuite } from "./runner-core.mjs";

const e2eRoot = path.resolve(fileURLToPath(new URL("..", import.meta.url)));
const result = runAcceptanceSuite(process.argv.slice(2), {
  e2eRoot,
  nodePath: process.execPath,
  env: process.env,
  spawnSync,
});
process.stderr.write(
  `[E2E] ${result.mode} 已验证场景 ${result.scenarioExecutions.map((item) => `${item.tag}:${item.status}`).join(",")}，Playwright exit=${result.status}\n`,
);
if (result.status !== 0) {
  process.stderr.write(result.stderr || "[E2E] 业务场景失败，详情见故障证据目录。\n");
}
process.exitCode = result.status;
