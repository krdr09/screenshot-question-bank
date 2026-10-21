const expectedByMode = {
  quick: ["A", "B", "C", "D"],
  full: ["A", "B", "C", "D", "E"],
};

export function validateScenarioSelection(mode, scenarioTags, requestedScenario) {
  const expected =
    mode === "scenario"
      ? requestedScenario && /^[A-E]$/.test(requestedScenario)
        ? [requestedScenario]
        : null
      : expectedByMode[mode];
  if (!expected) throw new Error("E2E 模式只支持 quick、full 或 scenario");
  const executions = [...scenarioTags].map((item) =>
    typeof item === "string" ? { tag: item, status: "passed" } : item,
  );
  const actual = executions.map((execution) => execution.tag).sort();
  const wanted = [...expected].sort();
  if (actual.length !== wanted.length || actual.some((tag, index) => tag !== wanted[index])) {
    throw new Error(
      `场景计数或标签不符合预期：${mode} 应为 ${wanted.join(",")}，实际为 ${actual.join(",") || "0 项"}`,
    );
  }
  const skipped = executions.filter(
    (execution) => !execution.status || ["skipped", "not-run"].includes(execution.status),
  );
  if (skipped.length) {
    throw new Error(
      `场景已发现但未实际执行：${skipped.map((execution) => execution.tag).join(",")}`,
    );
  }
}

export function extractScenarioTags(report) {
  const tags = [];
  const visit = (value) => {
    if (!value || typeof value !== "object") return;
    if (typeof value.title === "string" && Array.isArray(value.tests)) {
      const matches = [...value.title.matchAll(/@([A-E])\b/g)];
      for (const match of matches) tags.push(match[1]);
    }
    for (const child of Object.values(value)) {
      if (Array.isArray(child)) child.forEach(visit);
      else if (child && typeof child === "object") visit(child);
    }
  };
  visit(report);
  return tags;
}

export function extractScenarioExecutions(_report) {
  const executions = [];
  const visit = (value) => {
    if (!value || typeof value !== "object") return;
    if (typeof value.title === "string" && Array.isArray(value.tests)) {
      const tags = [...value.title.matchAll(/@([A-E])\b/g)].map((match) => match[1]);
      for (const tag of tags) {
        for (const test of value.tests) {
          const results = Array.isArray(test.results) ? test.results : [];
          executions.push({
            tag,
            status: results.at(-1)?.status ?? "not-run",
          });
        }
      }
    }
    for (const child of Object.values(value)) {
      if (Array.isArray(child)) child.forEach(visit);
      else if (child && typeof child === "object") visit(child);
    }
  };
  visit(_report);
  return executions;
}
