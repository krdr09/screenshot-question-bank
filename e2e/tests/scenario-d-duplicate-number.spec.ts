import type { Page } from "@playwright/test";

import { test, expect } from "../support/scenario-fixture";
import { importFolderThroughUi, waitForImportJob } from "../support/ui";

test.use({ scenarioName: "D" });

test("场景 D：相同来源题号保持独立 UUID、批次编辑和判分 @D", async ({ scenario }) => {
  const { page, data } = scenario;
  const common = { course: "场景 D 课程", category: "重复题号/隔离" };
  const firstBatch = "场景 D 批次一";
  const secondBatch = "场景 D 批次二";

  await page.goto("/");
  const firstJobId = await importFolderThroughUi(page, data.importDirectories[0], {
    ...common,
    batch: firstBatch,
  });
  const firstJob = await waitForImportJob(page, firstJobId, ["ready"]);
  expect(firstJob).toMatchObject({ found_question_count: 1, review_count: 0 });

  const secondJobId = await importFolderThroughUi(page, data.importDirectories[1], {
    ...common,
    batch: secondBatch,
  });
  const secondJob = await waitForImportJob(page, secondJobId, ["ready"]);
  expect(secondJob).toMatchObject({ found_question_count: 1, review_count: 0 });
  expect(secondJobId).not.toBe(firstJobId);

  await page.getByRole("button", { name: "打开题库管理" }).click();
  let library = page.locator(".library-launcher");
  await expect(library.getByText("2 道可用题目", { exact: true })).toBeVisible();
  let list = library.getByTestId("library-question-grid");
  let firstButton = list.getByRole("button", { name: /批次一独立题目/ });
  let secondButton = list.getByRole("button", { name: /批次二独立题目/ });
  await expect(firstButton).toContainText(firstBatch);
  await expect(secondButton).toContainText(secondBatch);

  await firstButton.click();
  const identifiers = library.locator(".library-identifiers");
  await expect(identifiers.locator("dd").nth(1)).toHaveText("1");
  const firstUuid = (await identifiers.locator("code").textContent())?.trim() ?? "";
  expect(firstUuid).toMatch(/^[0-9a-f-]{36}$/);
  await expect(library.getByLabel("正确答案")).toHaveValue("A");

  await secondButton.click();
  await expect(identifiers.locator("dd").nth(1)).toHaveText("1");
  const secondUuid = (await identifiers.locator("code").textContent())?.trim() ?? "";
  expect(secondUuid).toMatch(/^[0-9a-f-]{36}$/);
  expect(secondUuid).not.toBe(firstUuid);
  await expect(library.getByLabel("正确答案")).toHaveValue("B");
  await expect(list).not.toContainText(firstUuid);
  await expect(list).not.toContainText(secondUuid);

  await firstButton.click();
  await library.getByLabel("编辑题干").fill("批次一独立题目（已编辑）");
  const saveResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "PATCH" && /\/api\/questions\/[^/]+$/.test(response.url()),
  );
  await library.getByRole("button", { name: "保存题目" }).click();
  expect((await saveResponsePromise).status()).toBe(200);

  await page.reload();
  await page.getByRole("button", { name: "打开题库管理" }).click();
  library = page.locator(".library-launcher");
  await expect(library.getByText("2 道可用题目", { exact: true })).toBeVisible();
  list = library.getByTestId("library-question-grid");
  firstButton = list.getByRole("button", { name: /批次一独立题目（已编辑）/ });
  secondButton = list.getByRole("button", { name: /批次二独立题目/ });
  await expect(firstButton).toContainText(firstBatch);
  await expect(secondButton).toContainText(secondBatch);
  await expect(list).not.toContainText("批次二独立题目（已编辑）");

  await firstButton.click();
  await expect(library.locator(".library-identifiers code")).toHaveText(firstUuid);
  await expect(library.locator(".library-identifiers dd").nth(1)).toHaveText("1");
  await expect(library.getByLabel("正确答案")).toHaveValue("A");
  await secondButton.click();
  await expect(library.locator(".library-identifiers code")).toHaveText(secondUuid);
  await expect(library.locator(".library-identifiers dd").nth(1)).toHaveText("1");
  await expect(library.getByLabel("编辑题干")).toHaveValue("批次二独立题目");
  await expect(library.getByLabel("正确答案")).toHaveValue("B");

  await page.reload();
  await completeBatchPractice(page, common, firstBatch, "批次一独立题目（已编辑）", /^A\s*一$/);
  await completeBatchPractice(page, common, secondBatch, "批次二独立题目", /^B\s*四$/);
});

async function completeBatchPractice(
  page: Page,
  common: { course: string; category: string },
  batch: string,
  stem: string,
  answer: RegExp,
): Promise<void> {
  const returnToSettings = page.getByRole("button", { name: "返回刷题设置" });
  if ((await returnToSettings.count()) > 0) {
    await returnToSettings.click();
  } else {
    await page.getByRole("button", { name: "开始刷题" }).click();
  }
  const practice = page.locator(".practice-workspace");
  await practice.getByLabel("课程").selectOption({ label: common.course });
  await practice.getByLabel("分类/章节").selectOption({ label: common.category });
  await practice.getByLabel("导入批次").selectOption({ label: batch });
  await practice.getByLabel("整套完成后反馈").check();
  await expect(practice.getByText("当前匹配 1 题", { exact: true })).toBeVisible();
  await practice.getByRole("button", { name: "开始本次刷题" }).click();
  await expect(practice.getByRole("heading", { name: stem })).toBeVisible();
  await practice.getByLabel(answer).check();
  page.once("dialog", async (dialog) => {
    expect(dialog.message()).toContain("当前有 0 题未作答");
    await dialog.accept();
  });
  const submitResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      /\/api\/practice-sessions\/[^/]+\/submit$/.test(response.url()),
  );
  await practice.getByRole("button", { name: "提交整套答案" }).click();
  expect((await submitResponsePromise).status()).toBe(200);
  const summary = practice.getByTestId("practice-result-summary");
  await expect(summary).toContainText("总题数 1");
  await expect(summary).toContainText("正确 1");
  await expect(summary).toContainText("错误 0");
  await expect(summary).toContainText("正确率 100.00%");
}
