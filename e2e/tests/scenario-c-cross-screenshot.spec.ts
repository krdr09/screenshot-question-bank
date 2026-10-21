import { test, expect } from "../support/scenario-fixture";
import { importFolderThroughUi, waitForImportJob } from "../support/ui";

test.use({ scenarioName: "C" });

test("场景 C：跨截图候选确认前不可用，合并后保留两段图片并可判分 @C", async ({ scenario }) => {
  const { page, data } = scenario;
  const mapping = {
    course: "场景 C 课程",
    category: "跨截图/合并",
    batch: "场景 C 批次",
  };

  await page.goto("/");
  const jobId = await importFolderThroughUi(page, data.importDirectories[0], mapping);
  let job = await waitForImportJob(page, jobId, ["ordering_review", "question_review"]);
  if (job.status === "ordering_review") {
    await page.getByRole("button", { name: "确认并保存顺序" }).click();
    job = await waitForImportJob(page, jobId, ["question_review"]);
  }
  expect(job.status).toBe("question_review");
  const importCard = page.getByRole("article", { name: `导入任务 ${jobId}` });
  await expect(importCard).toContainText("跨截图建议");
  await expect(importCard).toContainText("等待人工决定");

  await page.getByRole("button", { name: "打开题库管理" }).click();
  const libraryBefore = page.locator(".library-launcher");
  await expect(libraryBefore.getByRole("heading", { name: "题库管理", level: 3 })).toBeVisible();
  await expect(libraryBefore.getByText("0 道可用题目", { exact: true })).toBeVisible();
  await expect(libraryBefore.getByText("当前筛选条件下没有正式题目。", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "开始刷题" }).click();
  const practiceBefore = page.locator(".practice-workspace");
  await practiceBefore.getByLabel("课程").selectOption({ label: mapping.course });
  await practiceBefore.getByLabel("分类/章节").selectOption({ label: mapping.category });
  await practiceBefore.getByLabel("导入批次").selectOption({ label: mapping.batch });
  await expect(practiceBefore.getByText("当前匹配 0 题", { exact: true })).toBeVisible();
  await expect(practiceBefore.getByRole("button", { name: "开始本次刷题" })).toBeDisabled();

  await page.getByRole("button", { name: "打开待检查页面" }).click();
  const review = page.locator(".review-launcher");
  await expect(review.getByRole("heading", { name: "异常题人工检查" })).toBeVisible();
  await review.getByRole("button", { name: /跨截图建议/ }).click();
  const relationship = review.getByRole("region", { name: "跨截图建议详情" });
  await expect(relationship).toBeVisible();
  await expect(relationship.getByRole("heading", { name: "截图 A / B 对照" })).toBeVisible();
  await expect(relationship.locator(".relationship-preview")).toContainText("跨截图合成题的前半部分");
  await expect(relationship.getByAltText(/^截图 A：/)).toBeVisible();
  await expect(relationship.getByAltText(/^截图 B：/)).toBeVisible();
  await expect(relationship.getByAltText("截图 A 建议裁切")).toBeVisible();
  await expect(relationship.getByAltText("截图 B 建议裁切")).toBeVisible();

  page.once("dialog", async (dialog) => {
    expect(dialog.message()).toBe("确认合并这两个截图片段？");
    await dialog.accept();
  });
  const mergeResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" && /\/api\/review-items\/[^/]+\/merge$/.test(response.url()),
  );
  await relationship.getByRole("button", { name: "确认跨截图合并" }).click();
  expect((await mergeResponsePromise).status()).toBe(200);

  // 合并保留在人工复核态，必须由用户再确认字段后才进入正式题库。
  await expect(review.getByLabel("题目字段编辑")).toBeVisible();
  await expect(review.getByRole("button", { name: "片段 1" })).toBeVisible();
  await expect(review.getByRole("button", { name: "片段 2" })).toBeVisible();
  await expect(review.getByLabel("正确答案")).toHaveValue("A");
  const confirmResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" && /\/api\/review-items\/[^/]+\/confirm$/.test(response.url()),
  );
  await review.getByRole("button", { name: "确认并入库" }).click();
  expect((await confirmResponsePromise).status()).toBe(200);

  await page.reload();
  const readyJob = await waitForImportJob(page, jobId, ["ready"], 30_000, false);
  expect(readyJob.status).toBe("ready");
  await page.getByRole("button", { name: "打开题库管理" }).click();
  const library = page.locator(".library-launcher");
  await expect(library.getByText("1 道可用题目", { exact: true })).toBeVisible();
  await expect(library.getByTestId("library-question-grid")).toContainText("跨截图合成题的前半部分");
  const cropImages = library.getByRole("img", { name: /^题目裁切图 / });
  const sourceImages = library.getByRole("img", { name: /^来源截图 / });
  await expect(cropImages).toHaveCount(2);
  await expect(sourceImages).toHaveCount(2);
  for (const image of [...await cropImages.all(), ...await sourceImages.all()]) {
    await expect
      .poll(() =>
        image.evaluate(
          (element: HTMLImageElement) =>
            element.complete && element.naturalWidth > 0 && element.naturalHeight > 0,
        ),
      )
      .toBe(true);
  }

  await page.reload();
  await page.getByRole("button", { name: "开始刷题" }).click();
  const practice = page.locator(".practice-workspace");
  await practice.getByLabel("课程").selectOption({ label: mapping.course });
  await practice.getByLabel("分类/章节").selectOption({ label: mapping.category });
  await practice.getByLabel("导入批次").selectOption({ label: mapping.batch });
  await practice.getByLabel("整套完成后反馈").check();
  await expect(practice.getByText("当前匹配 1 题", { exact: true })).toBeVisible();
  await practice.getByRole("button", { name: "开始本次刷题" }).click();
  await expect(practice.getByRole("heading", { name: /跨截图合成题的前半部分/ })).toBeVisible();
  const practiceImages = practice.getByRole("img", { name: /^题目图片 / });
  if ((await practiceImages.count()) === 0) {
    await practice.getByRole("button", { name: "查看题图" }).click();
  }
  await expect(practiceImages).toHaveCount(2);
  for (const image of await practiceImages.all()) {
    await expect
      .poll(() =>
        image.evaluate(
          (element: HTMLImageElement) =>
            element.complete && element.naturalWidth > 0 && element.naturalHeight > 0,
        ),
      )
      .toBe(true);
  }
  await practice.getByLabel(/^A\s*甲$/).check();
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
});
