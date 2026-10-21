import { test, expect } from "../support/scenario-fixture";
import {
  assertPayloadsHaveNoAnswerLeakage,
  importFolderThroughUi,
  waitForImportJob,
} from "../support/ui";

test.use({ scenarioName: "B" });

test("场景 B：缺答案题经人工审核后持久化并参与真实判分 @B", async ({ scenario }) => {
  const { page, data } = scenario;
  const mapping = {
    course: "场景 B 课程",
    category: "异常审核/缺答案",
    batch: "场景 B 批次",
  };

  await page.goto("/");
  const jobId = await importFolderThroughUi(page, data.importDirectories[0], mapping);
  const reviewJob = await waitForImportJob(page, jobId, ["question_review"]);
  expect(reviewJob).toMatchObject({
    status: "question_review",
    uploaded_count: 1,
    processed_count: 1,
    found_question_count: 1,
    review_count: 1,
    error_count: 0,
  });
  const importCard = page.getByRole("article", { name: `导入任务 ${jobId}` });
  await expect(importCard).toContainText("待检查 1");
  await expect(importCard).toContainText("未找到正确答案");

  await page.getByRole("button", { name: "打开待检查页面" }).click();
  const review = page.locator(".review-launcher");
  await expect(review.getByRole("heading", { name: "异常题人工检查" })).toBeVisible();
  await review.getByLabel("异常类型筛选").selectOption({ label: "未找到正确答案" });
  await expect(review.getByRole("button", { name: /这道合成题缺少答案/ })).toBeVisible();
  await expect(review.locator(".anomaly-list")).toContainText("未找到正确答案");
  await expect(review.getByLabel("正确答案")).toHaveValue("");

  await review.getByLabel("正确答案").selectOption("A");
  await review.getByLabel("解析文本").fill("人工补充：正确选项为甲");
  const saveResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "PATCH" && /\/api\/review-items\/[^/]+$/.test(response.url()),
  );
  await review.getByRole("button", { name: "保存修改" }).click();
  expect((await saveResponsePromise).status()).toBe(200);
  await expect(review.getByLabel("正确答案")).toHaveValue("A");

  const confirmResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" && /\/api\/review-items\/[^/]+\/confirm$/.test(response.url()),
  );
  await review.getByRole("button", { name: "确认并入库" }).click();
  expect((await confirmResponsePromise).status()).toBe(200);
  await expect(review.getByText("当前筛选条件下没有待处理内容。", { exact: true })).toBeVisible();

  // 重新加载整个生产页面，再从题库读取，避免只验证 React 临时状态。
  await page.reload();
  const readyJob = await waitForImportJob(page, jobId, ["ready"], 30_000, false);
  expect(readyJob).toMatchObject({ status: "ready", found_question_count: 1, review_count: 0 });
  await page.getByRole("button", { name: "打开题库管理" }).click();
  const library = page.locator(".library-launcher");
  await expect(library.getByRole("heading", { name: "题库管理", level: 3 })).toBeVisible();
  await expect(library.getByText("1 道可用题目", { exact: true })).toBeVisible();
  await expect(library.getByTestId("library-question-grid")).toContainText("这道合成题缺少答案");
  await expect(library.getByLabel("编辑题干")).toHaveValue("这道合成题缺少答案");
  await expect(library.getByLabel("正确答案")).toHaveValue("A");
  await expect(library.locator(".library-explanation")).toContainText("人工补充：正确选项为甲");

  await page.reload();
  await page.getByRole("button", { name: "开始刷题" }).click();
  const practice = page.locator(".practice-workspace");
  await practice.getByLabel("课程").selectOption({ label: mapping.course });
  await practice.getByLabel("分类/章节").selectOption({ label: mapping.category });
  await practice.getByLabel("导入批次").selectOption({ label: mapping.batch });
  await practice.getByLabel("整套完成后反馈").check();
  await expect(practice.getByText("当前匹配 1 题", { exact: true })).toBeVisible();
  const createResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" && /\/api\/practice-sessions$/.test(response.url()),
  );
  await practice.getByRole("button", { name: "开始本次刷题" }).click();
  const createPayload = await (await createResponsePromise).json();
  await expect(practice.getByRole("heading", { name: "这道合成题缺少答案" })).toBeVisible();
  const answerResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" && /\/api\/practice-sessions\/[^/]+\/answers$/.test(response.url()),
  );
  await practice.getByLabel(/^A\s*甲$/).check();
  const answerPayload = await (await answerResponsePromise).json();
  assertPayloadsHaveNoAnswerLeakage(
    [createPayload, answerPayload],
    ["correct_answer", "is_correct", "explanation", "answer_snapshot"],
  );

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
  await expect(practice.getByRole("heading", { name: "本次结果" })).toBeVisible();
  const summary = practice.getByTestId("practice-result-summary");
  await expect(summary).toContainText("总题数 1");
  await expect(summary).toContainText("正确 1");
  await expect(summary).toContainText("错误 0");
  await expect(summary).toContainText("正确率 100.00%");
  await expect(practice).toContainText("人工补充：正确选项为甲");
});
