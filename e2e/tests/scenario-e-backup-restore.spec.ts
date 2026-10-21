import { createHash } from "node:crypto";
import { stat } from "node:fs/promises";
import path from "node:path";

import { type Locator, type Page } from "@playwright/test";

import { test, expect } from "../support/scenario-fixture";
import { importFolderThroughUi, waitForImportJob } from "../support/ui";

test.use({ scenarioName: "E" });

interface ImageSignature {
  path: string;
  sha256: string;
  url: string;
}

async function expectImagesReadable(page: Page, images: Locator): Promise<ImageSignature[]> {
  const count = await images.count();
  expect(count).toBeGreaterThan(0);
  const signatures: ImageSignature[] = [];
  for (const image of await images.all()) {
    await expect.poll(() => image.evaluate((node: HTMLImageElement) =>
      node.complete && node.naturalWidth > 0 && node.naturalHeight > 0,
    )).toBe(true);
    const source = await image.getAttribute("src");
    if (!source) throw new Error("图片元素缺少 src");
    const response = await page.request.get(source);
    expect(response.status()).toBe(200);
    const body = await response.body();
    expect(body.byteLength).toBeGreaterThan(0);
    signatures.push({
      path: new URL(source, page.url()).pathname,
      sha256: createHash("sha256").update(body).digest("hex"),
      url: source,
    });
  }
  return signatures.sort((left, right) => left.path.localeCompare(right.path));
}

function stableImageIdentity(signatures: ImageSignature[]) {
  return signatures
    .map(({ path: imagePath, sha256 }) => ({ path: imagePath, sha256 }))
    .sort((left, right) => left.path.localeCompare(right.path));
}

async function openLibrary(page: Page) {
  await page.getByRole("button", { name: "打开题库管理" }).click();
  const library = page.locator(".library-launcher");
  await expect(library.getByRole("heading", { name: "题库管理", level: 3 })).toBeVisible();
  return library;
}

async function openWrongBook(page: Page) {
  await page.getByRole("button", { name: "打开错题本" }).click();
  const wrongBook = page.locator(".wrong-book-launcher");
  await expect(wrongBook.getByRole("heading", { name: "错题本" })).toBeVisible();
  return wrongBook;
}

test("场景 E：真实备份、全新状态、恢复重启和恢复后再次练习 @E", async ({ scenario }) => {
  let page = scenario.page;
  const { data, environment } = scenario;
  const mapping = {
    course: "场景 E 课程",
    category: "备份恢复/跨截图",
    batch: "场景 E 批次",
  };

  await page.goto("/");
  const jobId = await importFolderThroughUi(page, data.importDirectories[0], mapping);
  let job = await waitForImportJob(page, jobId, ["ordering_review", "question_review"]);
  if (job.status === "ordering_review") {
    await page.getByRole("button", { name: "确认并保存顺序" }).click();
    job = await waitForImportJob(page, jobId, ["question_review"]);
  }
  expect(job.status).toBe("question_review");

  await page.getByRole("button", { name: "打开待检查页面" }).click();
  const review = page.locator(".review-launcher");
  await review.getByRole("button", { name: /跨截图建议/ }).click();
  const relationship = review.getByRole("region", { name: "跨截图建议详情" });
  page.once("dialog", async (dialog) => {
    expect(dialog.message()).toBe("确认合并这两个截图片段？");
    await dialog.accept();
  });
  const mergeResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && /\/api\/review-items\/[^/]+\/merge$/.test(response.url()),
  );
  await relationship.getByRole("button", { name: "确认跨截图合并" }).click();
  expect((await mergeResponse).status()).toBe(200);
  await expect(review.getByLabel("正确答案")).toHaveValue("A");
  const confirmResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && /\/api\/review-items\/[^/]+\/confirm$/.test(response.url()),
  );
  await review.getByRole("button", { name: "确认并入库" }).click();
  expect((await confirmResponse).status()).toBe(200);
  await waitForImportJob(page, jobId, ["ready"], 30_000, false);

  const library = await openLibrary(page);
  await expect(library.getByText("2 道可用题目", { exact: true })).toBeVisible();
  await expect(library.getByLabel("课程筛选").locator("option", { hasText: mapping.course })).toHaveCount(1);
  await expect(library.getByLabel("分类筛选").locator("option", { hasText: mapping.category })).toHaveCount(1);
  await expect(library.getByLabel("批次筛选").locator("option", { hasText: mapping.batch })).toHaveCount(1);
  await library.getByTestId("library-question-grid").getByRole("button", { name: /跨截图合成题的前半部分/ }).click();
  const detail = library.getByRole("article", { name: "题目详情与编辑" });
  await expect(detail.getByLabel("选项 A 文本")).toHaveValue("甲");
  await expect(detail.getByLabel("选项 B 文本")).toHaveValue("乙");
  await expect(detail.getByLabel("正确答案")).toHaveValue("A");
  await expect(detail.getByRole("img", { name: /^题目裁切图 / })).toHaveCount(2);
  await expect(detail.getByRole("img", { name: /^来源截图 / })).toHaveCount(2);
  const crossImageSignatures = [
    ...await expectImagesReadable(page, detail.getByRole("img", { name: /^题目裁切图 / })),
    ...await expectImagesReadable(page, detail.getByRole("img", { name: /^来源截图 / })),
  ];
  await library.getByText("恢复后仍可继续作答", { exact: true }).click();
  await expect(detail.getByRole("img", { name: /^题目裁切图/ })).toHaveCount(1);
  await expect(detail.getByRole("img", { name: /^来源截图 / })).toHaveCount(1);
  const independentImageSignatures = [
    ...await expectImagesReadable(page, detail.getByRole("img", { name: /^题目裁切图/ })),
    ...await expectImagesReadable(page, detail.getByRole("img", { name: /^来源截图 / })),
  ];
  const oldImageUrls = [...crossImageSignatures, ...independentImageSignatures].map(
    ({ url }) => url,
  );

  await page.reload();
  await page.getByRole("button", { name: "开始刷题" }).click();
  const practice = page.locator(".practice-workspace");
  await practice.getByLabel("课程").selectOption({ label: mapping.course });
  await practice.getByLabel("分类/章节").selectOption({ label: mapping.category });
  await practice.getByLabel("导入批次").selectOption({ label: mapping.batch });
  await practice.getByLabel("整套完成后反馈").check();
  await expect(practice.getByText("当前匹配 2 题", { exact: true })).toBeVisible();
  await practice.getByRole("button", { name: "开始本次刷题" }).click();
  await expect(practice.getByRole("heading", { name: /恢复后仍可继续作答|跨截图合成题的前半部分/ })).toBeVisible();
  const answeredStems: string[] = [];
  for (let index = 0; index < 2; index += 1) {
    const heading = practice.getByRole("heading", { level: 2 });
    const stem = (await heading.textContent())?.trim() ?? "";
    expect(stem === "恢复后仍可继续作答" || stem.includes("跨截图合成题的前半部分")).toBe(true);
    answeredStems.push(stem);
    const saveResponse = page.waitForResponse((response) =>
      response.request().method() === "POST" && /\/api\/practice-sessions\/[^/]+\/answers$/.test(response.url()),
    );
    await practice.getByLabel(stem === "恢复后仍可继续作答" ? /^B\s*是$/ : /^B\s*乙$/).check();
    expect((await saveResponse).status()).toBe(200);
    if (index === 0) await practice.getByRole("button", { name: "下一题" }).click();
  }
  expect(answeredStems).toHaveLength(2);
  expect(answeredStems).toContain("恢复后仍可继续作答");
  expect(answeredStems.some((stem) => stem.includes("跨截图合成题的前半部分"))).toBe(true);
  page.once("dialog", async (dialog) => {
    expect(dialog.message()).toContain("当前有 0 题未作答");
    await dialog.accept();
  });
  const submitResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && /\/api\/practice-sessions\/[^/]+\/submit$/.test(response.url()),
  );
  await practice.getByRole("button", { name: "提交整套答案" }).click();
  expect((await submitResponse).status()).toBe(200);
  await expect(practice.getByTestId("practice-result-summary")).toContainText("正确 1");
  await expect(practice.getByTestId("practice-result-summary")).toContainText("错误 1");

  await practice.getByRole("button", { name: "进入错题本" }).click();
  let wrongBook = page.locator(".wrong-book-launcher");
  await expect(wrongBook.getByText("共 1 道错题", { exact: false })).toBeVisible();
  await expect(wrongBook.getByText("累计答错 1 次", { exact: true })).toBeVisible();
  await expect(wrongBook.getByText(/跨截图合成题的前半部分/).first()).toBeVisible();
  await expect(wrongBook.getByRole("img", { name: /^错题裁切图 / })).toHaveCount(2);
  await expectImagesReadable(page, wrongBook.getByRole("img", { name: /^错题裁切图 / }));

  await page.getByRole("button", { name: "查看练习历史" }).click();
  const history = page.locator(".practice-history");
  await expect(history.getByText("共 1 次", { exact: true })).toBeVisible();
  await expect(history.getByText(/已提交 · 正确 1 · 错误 1 · 未答 0/)).toBeVisible();

  await page.getByRole("button", { name: "打开备份与恢复" }).click();
  let backup = page.locator(".backup-launcher");
  const createResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith("/api/backups"),
  );
  await backup.getByRole("button", { name: "创建完整备份" }).click();
  expect((await createResponse).status()).toBe(201);
  const backupArticle = backup.locator(".backup-list article").first();
  // 完整备份包含 2 道正式题和被合并后保留审计记录的次候选题。
  await expect(backupArticle).toContainText("3 道题目");
  const downloadPromise = page.waitForEvent("download");
  await backupArticle.getByRole("link", { name: /^下载 / }).click();
  const download = await downloadPromise;
  const backupZip = path.join(environment.exportedBackupDir, path.basename(download.suggestedFilename()));
  await download.saveAs(backupZip);
  expect(await download.failure()).toBeNull();
  expect((await stat(backupZip)).size).toBeGreaterThan(0);
  await backup.getByLabel("选择外部 ZIP 备份").setInputFiles(backupZip);
  await backup.getByRole("button", { name: "验证备份" }).click();
  await expect(backup.getByRole("heading", { name: "验证摘要" })).toBeVisible();
  await expect(backup.getByText("文件哈希全部通过", { exact: true })).toBeVisible();

  await scenario.stopService();
  await scenario.clearApplicationDataPreservingExternalBackup(backupZip);
  await scenario.startService();
  page = await scenario.replaceBrowserContext();
  await page.goto("/");

  const emptyLibrary = await openLibrary(page);
  await expect(emptyLibrary.getByText("0 道可用题目", { exact: true })).toBeVisible();
  wrongBook = await openWrongBook(page);
  await expect(wrongBook.getByText("共 0 道错题", { exact: false })).toBeVisible();
  await expect(wrongBook.getByText("当前筛选下没有错题。", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "查看练习历史" }).click();
  await expect(page.locator(".practice-history").getByText("共 0 次", { exact: true })).toBeVisible();
  await expect(page.getByText("还没有历史练习。完成或保存一次刷题后会显示在这里。")).toBeVisible();
  for (const oldUrl of oldImageUrls) {
    expect((await page.request.get(oldUrl)).status()).toBe(404);
  }

  await page.getByRole("button", { name: "打开备份与恢复" }).click();
  backup = page.locator(".backup-launcher");
  await backup.getByLabel("选择外部 ZIP 备份").setInputFiles(backupZip);
  await backup.getByRole("button", { name: "验证备份" }).click();
  await expect(backup.getByRole("heading", { name: "验证摘要" })).toBeVisible();
  await expect(backup.getByText("文件哈希全部通过", { exact: true })).toBeVisible();
  await expect(backup.locator("dt", { hasText: /^题目$/ }).locator("..").locator("dd")).toHaveText("3");
  await backup.getByLabel(/已了解恢复会替换当前题库/).check();
  page.once("dialog", async (dialog) => {
    expect(dialog.message()).toBe("确认使用已验证的备份替换当前题库？系统会先创建安全备份，成功后需要重启应用。");
    await dialog.accept();
  });
  const restoreResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith("/api/backups/restore"),
  );
  await backup.getByRole("button", { name: "确认恢复" }).click();
  expect((await restoreResponse).status()).toBe(200);
  await expect(backup.getByRole("heading", { name: "恢复已完成，请重启应用" })).toBeVisible();
  await expect(backup.getByText("当前进程已停止接受题库操作。请关闭并重新启动应用后继续使用。")).toBeVisible();

  await scenario.stopService();
  await scenario.startService();
  page = await scenario.replaceBrowserContext();
  await page.goto("/");

  const restoredLibrary = await openLibrary(page);
  await expect(restoredLibrary.getByText("2 道可用题目", { exact: true })).toBeVisible();
  await expect(restoredLibrary.getByLabel("课程筛选").locator("option", { hasText: mapping.course })).toHaveCount(1);
  await expect(restoredLibrary.getByLabel("分类筛选").locator("option", { hasText: mapping.category })).toHaveCount(1);
  await expect(restoredLibrary.getByLabel("批次筛选").locator("option", { hasText: mapping.batch })).toHaveCount(1);
  await restoredLibrary.getByTestId("library-question-grid").getByRole("button", { name: /跨截图合成题的前半部分/ }).click();
  const restoredDetail = restoredLibrary.getByRole("article", { name: "题目详情与编辑" });
  await expect(restoredDetail.getByLabel("选项 A 文本")).toHaveValue("甲");
  await expect(restoredDetail.getByLabel("选项 B 文本")).toHaveValue("乙");
  await expect(restoredDetail.getByLabel("正确答案")).toHaveValue("A");
  await expect(restoredDetail.getByRole("img", { name: /^题目裁切图 / })).toHaveCount(2);
  await expect(restoredDetail.getByRole("img", { name: /^来源截图 / })).toHaveCount(2);
  await expectImagesReadable(page, restoredDetail.getByRole("img", { name: /^题目裁切图 / }));
  await expectImagesReadable(page, restoredDetail.getByRole("img", { name: /^来源截图 / }));
  await restoredLibrary.getByTestId("library-question-grid").getByRole("button", { name: "恢复后仍可继续作答" }).click();
  await expect(restoredDetail.getByLabel("选项 A 文本")).toHaveValue("否");
  await expect(restoredDetail.getByLabel("选项 B 文本")).toHaveValue("是");
  await expect(restoredDetail.getByLabel("正确答案")).toHaveValue("B");
  await expectImagesReadable(page, restoredDetail.getByRole("img", { name: /^题目裁切图 / }));
  await expectImagesReadable(page, restoredDetail.getByRole("img", { name: /^来源截图 / }));

  await restoredLibrary
    .getByTestId("library-question-grid")
    .getByRole("button", { name: /跨截图合成题的前半部分/ })
    .click();
  await expect(restoredDetail.getByRole("img", { name: /^题目裁切图/ })).toHaveCount(2);
  await expect(restoredDetail.getByRole("img", { name: /^来源截图 / })).toHaveCount(2);
  const restoredCrossImages = [
    ...await expectImagesReadable(page, restoredDetail.getByRole("img", { name: /^题目裁切图/ })),
    ...await expectImagesReadable(page, restoredDetail.getByRole("img", { name: /^来源截图 / })),
  ];
  expect(stableImageIdentity(restoredCrossImages)).toEqual(
    stableImageIdentity(crossImageSignatures),
  );
  await restoredLibrary.getByText("恢复后仍可继续作答", { exact: true }).click();
  await expect(restoredDetail.getByRole("img", { name: /^题目裁切图/ })).toHaveCount(1);
  await expect(restoredDetail.getByRole("img", { name: /^来源截图 / })).toHaveCount(1);
  const restoredIndependentImages = [
    ...await expectImagesReadable(page, restoredDetail.getByRole("img", { name: /^题目裁切图/ })),
    ...await expectImagesReadable(page, restoredDetail.getByRole("img", { name: /^来源截图 / })),
  ];
  expect(stableImageIdentity(restoredIndependentImages)).toEqual(
    stableImageIdentity(independentImageSignatures),
  );

  wrongBook = await openWrongBook(page);
  await expect(wrongBook.getByText("共 1 道错题", { exact: false })).toBeVisible();
  await expect(wrongBook.getByText("累计答错 1 次", { exact: true })).toBeVisible();
  await expect(wrongBook.getByRole("img", { name: /^错题裁切图 / })).toHaveCount(2);
  await expectImagesReadable(page, wrongBook.getByRole("img", { name: /^错题裁切图 / }));
  await page.getByRole("button", { name: "查看练习历史" }).click();
  await expect(page.locator(".practice-history").getByText("共 1 次", { exact: true })).toBeVisible();
  await expect(page.locator(".practice-history").getByText(/已提交 · 正确 1 · 错误 1 · 未答 0/)).toBeVisible();

  await wrongBook.getByLabel(/选择 跨截图合成题的前半部分/).check();
  await wrongBook.getByRole("button", { name: "刷选中错题" }).click();
  const restoredPractice = page.locator(".practice-workspace");
  await expect(restoredPractice.getByRole("heading", { name: "刷题设置" })).toBeVisible();
  await expect(restoredPractice.getByText(/当前匹配 2 题，已选 1 题/)).toBeVisible();
  await restoredPractice.getByRole("button", { name: "开始本次刷题" }).click();
  await expect(restoredPractice.getByRole("heading", { name: /跨截图合成题的前半部分/ })).toBeVisible();
  await restoredPractice.getByLabel(/^A\s*甲$/).check();
  const answerResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && /\/api\/practice-sessions\/[^/]+\/answers$/.test(response.url()),
  );
  await restoredPractice.getByRole("button", { name: "提交本题" }).click();
  expect((await answerResponse).status()).toBe(200);
  await expect(restoredPractice.getByText("回答正确", { exact: true })).toBeVisible();
  page.once("dialog", async (dialog) => {
    expect(dialog.message()).toContain("当前有 0 题未作答");
    await dialog.accept();
  });
  const secondSubmit = page.waitForResponse((response) =>
    response.request().method() === "POST" && /\/api\/practice-sessions\/[^/]+\/submit$/.test(response.url()),
  );
  await restoredPractice.getByRole("button", { name: "提交整套答案" }).click();
  expect((await secondSubmit).status()).toBe(200);
  await expect(restoredPractice.getByTestId("practice-result-summary")).toContainText("正确 1");

  await page.reload();
  await expect(page.locator(".practice-workspace").getByRole("heading", { name: "本次结果" })).toBeVisible();
  await page.getByRole("button", { name: "查看练习历史" }).click();
  const restoredHistory = page.locator(".practice-history");
  await expect(restoredHistory.getByText("共 2 次", { exact: true })).toBeVisible();
  await expect(restoredHistory.locator(".practice-history-list article")).toHaveCount(2);
});
