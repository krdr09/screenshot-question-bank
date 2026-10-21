import path from "node:path";

import { extractScenarioExecutions, validateScenarioSelection } from "./suite-selection.mjs";

export function runAcceptanceSuite(arguments_, options) {
  const [mode, ...remaining] = arguments_;
  if (!mode) throw new Error("缺少 E2E 模式：quick、full 或 scenario");
  if (!["quick", "full", "scenario"].includes(mode)) {
    throw new Error("E2E 模式只支持 quick、full 或 scenario");
  }
  let requestedScenario;
  if (mode === "scenario") {
    const scenarioIndex = remaining.indexOf("--scenario");
    if (scenarioIndex >= 0) requestedScenario = remaining[scenarioIndex + 1]?.toUpperCase();
    const grepIndex = remaining.indexOf("--grep");
    if (!requestedScenario && grepIndex >= 0) {
      requestedScenario = remaining[grepIndex + 1]?.match(/@?([A-E])/i)?.[1].toUpperCase();
    }
    if (!requestedScenario || !/^[A-E]$/.test(requestedScenario)) {
      throw new Error("单场景命令必须提供 --scenario A 至 E（或 --grep @A 至 @E）");
    }
  }
  const project = mode === "quick" ? "quick" : "full";
  const cli = path.join(options.e2eRoot, "node_modules", "@playwright", "test", "cli.js");
  const playwrightArguments = [
    cli,
    "test",
    "--config",
    path.join(options.e2eRoot, "playwright.config.ts"),
    "--project",
    project,
    "--reporter",
    "json",
  ];
  if (requestedScenario) playwrightArguments.push("--grep", `@${requestedScenario}\\b`);
  const result = options.spawnSync(options.nodePath, playwrightArguments, {
    cwd: options.e2eRoot,
    env: options.env,
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
    windowsHide: true,
  });
  if (result.error) throw result.error;
  let report;
  try {
    report = JSON.parse(result.stdout);
  } catch (error) {
    throw new Error(
      `Playwright 未返回有效 JSON 报告，不能确认测试数量：${result.stderr}`,
      { cause: error },
    );
  }
  const scenarioExecutions = extractScenarioExecutions(report);
  validateScenarioSelection(mode, scenarioExecutions, requestedScenario);
  return {
    mode,
    status: result.status ?? 1,
    stderr: result.stderr || "",
    scenarioExecutions,
  };
}
