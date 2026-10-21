import { test, expect } from "../support/scenario-fixture";
import {
  assertNoAnswerLeakage,
  importFolderThroughUi,
  waitForImportJob,
} from "../support/ui";

test.use({ scenarioName: "A" });

test("场景 A：正常导入三题并由真实后端判分 @A", async ({ scenario }) => {
  const { page, data } = scenario;
  const mapping = {
    course: "场景 A 课程",
    category: "正常导入/基础",
    batch: "场景 A 批次",
  };

  await page.goto("/");
  const jobId = await importFolderThroughUi(page, data.importDirectories[0], mapping);
  let job = await waitForImportJob(page, jobId, [
    "ordering_review",
    "question_review",
    "ready",
    "partially_ready",
  ]);
  if (job.status === "ordering_review") {
    await page.getByRole("button", { name: "确认并保存顺序" }).click();
    job = await waitForImportJob(page, jobId, ["question_review", "ready", "partially_ready"]);
  }

  expect(job.status).toBe("ready");
  expect(job).toMatchObject({
    uploaded_count: 3,
    processed_count: 3,
    found_question_count: 3,
    review_count: 0,
    error_count: 0,
  });
  const importCard = page.getByRole("article", { name: `导入任务 ${jobId}` });
  await expect(importCard.getByRole("heading", { name: "处理完成" })).toBeVisible();
  await expect(importCard).toContainText("3 / 3");
  await expect(importCard).toContainText("已找到 3");
  await expect(importCard).toContainText("Mock OCR");

  await page.getByRole("button", { name: "打开题库管理" }).click();
  await expect(page.getByRole("heading", { name: "题库管理", level: 3 })).toBeVisible();
  await expect(page.getByText("3 道可用题目", { exact: true })).toBeVisible();
  const libraryList = page.getByTestId("library-question-grid");
  await expect(libraryList.getByRole("button")).toHaveCount(3);
  await expect(libraryList).toContainText("请选择第五项");
  await expect(libraryList).toContainText("两项中选择乙");
  await expect(libraryList).toContainText("太阳从东方升起");
  await expect(libraryList.locator("button", { hasText: "单选" })).toHaveCount(2);
  await expect(libraryList.locator("button", { hasText: "判断" })).toHaveCount(1);

  // 题库详情合法展示答案；重新加载以证明刷题页面本身没有遗留或预载答案。
  await page.reload();
  await page.getByRole("button", { name: "开始刷题" }).click();
  const practice = page.locator(".practice-workspace");
  await expect(practice.getByRole("heading", { name: "刷题设置" })).toBeVisible();
  await practice.getByLabel("课程").selectOption({ label: mapping.course });
  await practice.getByLabel("分类/章节").selectOption({ label: mapping.category });
  await practice.getByLabel("导入批次").selectOption({ label: mapping.batch });
  await practice.getByLabel("整套完成后反馈").check();
  await expect(practice.getByText("当前匹配 3 题", { exact: true })).toBeVisible();
  const preSubmitPayloads: unknown[] = [];
  const createResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" && /\/api\/practice-sessions$/.test(response.url()),
  );
  await practice.getByRole("button", { name: "开始本次刷题" }).click();
  preSubmitPayloads.push(await (await createResponsePromise).json());

  await expect(practice.getByRole("heading", { name: "请选择第五项" })).toBeVisible();
  let saveResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" && /\/api\/practice-sessions\/[^/]+\/answers$/.test(response.url()),
  );
  await practice.getByLabel(/^E\s*戊$/).check();
  preSubmitPayloads.push(await (await saveResponsePromise).json());
  await practice.getByRole("button", { name: "下一题" }).click();
  await expect(practice.getByRole("heading", { name: "两项中选择乙" })).toBeVisible();
  saveResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" && /\/api\/practice-sessions\/[^/]+\/answers$/.test(response.url()),
  );
  await practice.getByLabel(/^B\s*乙$/).check();
  preSubmitPayloads.push(await (await saveResponsePromise).json());
  await practice.getByRole("button", { name: "下一题" }).click();
  await expect(practice.getByRole("heading", { name: "太阳从东方升起" })).toBeVisible();
  const booleanAnswers = practice.getByLabel("判断题答案");
  await expect(booleanAnswers.getByRole("button", { name: "✓ 正确" })).toBeVisible();
  await expect(booleanAnswers.getByRole("button", { name: "✗ 错误" })).toBeVisible();
  await expect(booleanAnswers.getByRole("radio")).toHaveCount(0);
  await expect(booleanAnswers.getByText("A", { exact: true })).toHaveCount(0);
  await expect(booleanAnswers.getByText("B", { exact: true })).toHaveCount(0);
  saveResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" && /\/api\/practice-sessions\/[^/]+\/answers$/.test(response.url()),
  );
  await booleanAnswers.getByRole("button", { name: "✓ 正确" }).click();
  preSubmitPayloads.push(await (await saveResponsePromise).json());
  await expect(practice.locator(".practice-question-nav small", { hasText: "已保存" })).toHaveCount(3);

  await assertNoAnswerLeakage(page, {
    forbiddenVisibleText: ["正确答案：", "回答正确", "回答错误", "本次结果", "第五项是戊"],
    forbiddenDomTokens: [
      "correct_answer",
      "is_correct",
      "answer_snapshot",
      "explanation_snapshot",
      "第五项是戊",
    ],
    networkPayloads: preSubmitPayloads,
  });

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
  await expect(summary).toContainText("总题数 3");
  await expect(summary).toContainText("正确 3");
  await expect(summary).toContainText("错误 0");
  await expect(summary).toContainText("未作答 0");
  await expect(summary).toContainText("正确率 100.00%");
  await expect(practice.locator(".practice-result-state", { hasText: "正确" })).toHaveCount(3);
});
