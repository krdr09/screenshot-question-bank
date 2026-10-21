import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

import { test, expect } from "@playwright/test";

import acceptanceConfig from "../playwright.config";
import { buildFrontendProduction } from "../support/global-setup";
import {
  extractScenarioExecutions,
  validateScenarioSelection,
} from "../support/suite-selection.mjs";
import { runAcceptanceSuite } from "../support/runner-core.mjs";

test("suite selection requires exactly A-D for quick and A-E for full", () => {
  expect(() => validateScenarioSelection("quick", ["A", "B", "C", "D"])).not.toThrow();
  expect(() => validateScenarioSelection("full", ["A", "B", "C", "D", "E"])).not.toThrow();
  expect(() => validateScenarioSelection("quick", ["A", "B", "C", "D", "E"])).toThrow(
    "场景计数",
  );
  expect(() => validateScenarioSelection("full", [])).toThrow("场景计数");
  expect(() => validateScenarioSelection("scenario", ["C"], "C")).not.toThrow();
  expect(() => validateScenarioSelection("scenario", ["C", "D"], "C")).toThrow("场景计数");
  expect(() =>
    validateScenarioSelection("quick", [
      { tag: "A", status: "passed" },
      { tag: "B", status: "passed" },
      { tag: "C", status: "passed" },
      { tag: "D", status: "skipped" },
    ]),
  ).toThrow("未实际执行");
  expect(
    extractScenarioExecutions({
      suites: [
        {
          specs: [
            {
              title: "scenario A @A",
              tests: [{ results: [{ status: "passed" }] }],
            },
          ],
        },
      ],
    }),
  ).toEqual([{ tag: "A", status: "passed" }]);
});

test("JSON report extraction preserves skipped results for runner rejection", () => {
  const report = {
    suites: [
      {
        specs: [
          {
            title: "scenario D @D",
            tests: [{ results: [{ status: "skipped" }] }],
          },
        ],
      },
    ],
  };

  const executions = extractScenarioExecutions(report);
  expect(executions).toEqual([{ tag: "D", status: "skipped" }]);
  expect(() =>
    validateScenarioSelection("quick", [
      { tag: "A", status: "passed" },
      { tag: "B", status: "passed" },
      { tag: "C", status: "passed" },
      ...executions,
    ]),
  ).toThrow("未实际执行");
});

test("production runner rejects a zero-execution skipped scenario report", () => {
  const skippedReport = JSON.stringify({
    suites: [
      {
        specs: [
          ...["A", "B", "C"].map((tag) => ({
            title: `scenario ${tag} @${tag}`,
            tests: [{ results: [{ status: "passed" }] }],
          })),
          {
            title: "scenario D @D",
            tests: [{ results: [{ status: "skipped" }] }],
          },
        ],
      },
    ],
  });

  expect(() =>
    runAcceptanceSuite(["quick"], {
      e2eRoot: "C:\\controlled-e2e",
      nodePath: "node",
      env: {},
      spawnSync: () => ({ status: 0, stdout: skippedReport, stderr: "" }),
    }),
  ).toThrow("未实际执行");
});

test("Playwright quick and full projects use distinct A-D and A-E tags", () => {
  const projects = acceptanceConfig.projects ?? [];
  const quick = projects.find((project) => project.name === "quick");
  const full = projects.find((project) => project.name === "full");

  expect(projects.map((project) => project.name)).toEqual(["quick", "full"]);
  expect(quick?.grep).toBeInstanceOf(RegExp);
  expect(full?.grep).toBeInstanceOf(RegExp);
  expect((quick?.grep as RegExp).test("scenario @A")).toBeTruthy();
  expect((quick?.grep as RegExp).test("scenario @E")).toBeFalsy();
  expect((full?.grep as RegExp).test("scenario @E")).toBeTruthy();
});

test("global setup invokes one production build and requires dist index", async ({}, testInfo) => {
  const repoRoot = testInfo.outputPath("repo");
  let calls = 0;

  await buildFrontendProduction(repoRoot, async (receivedRoot) => {
    calls += 1;
    expect(receivedRoot).toBe(repoRoot);
    const dist = path.join(repoRoot, "frontend", "dist");
    await mkdir(dist, { recursive: true });
    await writeFile(path.join(dist, "index.html"), "<!doctype html>");
  });

  expect(calls).toBe(1);
});
