import { expect, type Page } from "@playwright/test";

export interface ImportMappingValues {
  course: string;
  category: string;
  batch: string;
}

export interface ImportJobState {
  id: string;
  status: string;
  uploaded_count: number;
  processed_count: number;
  found_question_count: number;
  review_count: number;
  error_count: number;
}

export interface AnswerLeakageRules {
  forbiddenVisibleText: string[];
  forbiddenDomTokens: string[];
  networkPayloads?: unknown[];
}

export function assertPayloadsHaveNoAnswerLeakage(
  payloads: unknown[],
  forbiddenTokens: string[],
): void {
  payloads.forEach((payload, index) => {
    const serialized = JSON.stringify(payload);
    for (const forbidden of forbiddenTokens) {
      expect(serialized, `提交前网络响应 ${index + 1} 提前泄露：${forbidden}`).not.toContain(
        forbidden,
      );
    }
  });
}

const statusLabels: Record<string, string> = {
  ordering_review: "等待顺序检查",
  question_review: "等待题目检查",
  ready: "处理完成",
  partially_ready: "部分完成",
  failed: "处理失败",
  cancelled: "已取消",
};

export async function importFolderThroughUi(
  page: Page,
  directory: string,
  mapping: ImportMappingValues,
): Promise<string> {
  await page.getByLabel("选择截图文件夹").setInputFiles(directory);
  const editor = page.locator("fieldset.mapping-card").first();
  await expect(editor).toBeVisible();
  await editor.getByLabel(/^课程(?: \d+)?$/).fill(mapping.course);
  await editor.getByLabel(/^分类\/章节(?: \d+)?$/).fill(mapping.category);
  await editor.getByLabel(/^批次(?: \d+)?$/).fill(mapping.batch);
  await page.getByRole("button", { name: "确认分类并创建导入任务" }).click();
  await expect(page.getByRole("status")).toContainText("已创建 1 个导入批次");
  const card = page.locator('article[aria-label^="导入任务 "]').last();
  await expect(card).toBeVisible();
  const accessibleName = await card.getAttribute("aria-label");
  const jobId = accessibleName?.match(/^导入任务 (.+)$/)?.[1];
  if (!jobId) throw new Error("UI 未显示导入任务标识");
  return jobId;
}

export async function waitForImportJob(
  page: Page,
  jobId: string,
  acceptedStatuses: string[],
  timeoutMs = 30_000,
  verifyProgressCard = true,
): Promise<ImportJobState> {
  let latest: ImportJobState | undefined;
  await expect
    .poll(
      async () => {
        const response = await page.request.get(`/api/import-jobs/${jobId}`);
        if (!response.ok()) throw new Error(`读取导入任务失败：HTTP ${response.status()}`);
        latest = (await response.json()) as ImportJobState;
        if (["failed", "cancelled"].includes(latest.status)) {
          throw new Error(`导入任务终止于 ${latest.status}`);
        }
        return acceptedStatuses.includes(latest.status);
      },
      { timeout: timeoutMs, message: `等待导入任务 ${jobId} 进入 ${acceptedStatuses.join("/")}` },
    )
    .toBe(true);
  if (!latest) throw new Error("导入任务轮询没有返回状态");
  const label = statusLabels[latest.status];
  if (label && verifyProgressCard) {
    const card = page.getByRole("article", { name: `导入任务 ${jobId}` });
    await expect(card.getByRole("heading", { name: label })).toBeVisible();
    await expect(card).toContainText(`${latest.processed_count} / ${latest.uploaded_count}`);
  }
  return latest;
}

export async function assertNoAnswerLeakage(
  page: Page,
  rules: AnswerLeakageRules,
): Promise<void> {
  const surfaces = await page.evaluate(() => {
    const attributes = Array.from(document.querySelectorAll("*"))
      .flatMap((element) =>
        Array.from(element.attributes).map(
          (attribute) => `${element.tagName.toLowerCase()} ${attribute.name}=${attribute.value}`,
        ),
      )
      .join("\n");
    const storage = {
      localStorage: Object.fromEntries(
        Array.from({ length: window.localStorage.length }, (_, index) => {
          const key = window.localStorage.key(index) ?? "";
          return [key, window.localStorage.getItem(key)];
        }),
      ),
      sessionStorage: Object.fromEntries(
        Array.from({ length: window.sessionStorage.length }, (_, index) => {
          const key = window.sessionStorage.key(index) ?? "";
          return [key, window.sessionStorage.getItem(key)];
        }),
      ),
      initialState: {
        __INITIAL_STATE__: (window as typeof window & { __INITIAL_STATE__?: unknown }).__INITIAL_STATE__,
        __PRELOADED_STATE__: (window as typeof window & { __PRELOADED_STATE__?: unknown })
          .__PRELOADED_STATE__,
        __NEXT_DATA__: (window as typeof window & { __NEXT_DATA__?: unknown }).__NEXT_DATA__,
      },
    };
    return {
      visibleText: document.body.innerText,
      domText: document.documentElement.textContent ?? "",
      attributes,
      embeddedScripts: Array.from(document.scripts)
        .map((script) => script.textContent ?? "")
        .join("\n"),
      storageAndInitialState: JSON.stringify(storage),
    };
  });

  for (const forbidden of rules.forbiddenVisibleText) {
    expect(surfaces.visibleText, `可见页面文本提前泄露：${forbidden}`).not.toContain(forbidden);
  }
  for (const [surfaceName, contents] of Object.entries(surfaces)) {
    for (const forbidden of rules.forbiddenDomTokens) {
      expect(contents, `${surfaceName} 提前泄露：${forbidden}`).not.toContain(forbidden);
    }
  }
  assertPayloadsHaveNoAnswerLeakage(rules.networkPayloads ?? [], rules.forbiddenDomTokens);
}
