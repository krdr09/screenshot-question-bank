import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { LibraryWorkspace } from "./LibraryWorkspace";
import type { LibraryQuestion } from "./api";


const courses = {
  items: [{ id: "course-1", name: "医学", categories: [{ id: "cat-1", path: "内科/呼吸" }, { id: "cat-2", path: "内科/循环" }] }],
};
const batches = {
  items: [{ id: "batch-1", name: "正式批次", status: "ready", course_id: "course-1", course_name: "医学", category_id: "cat-1", category_path: "内科/呼吸", question_count: 1 }],
};
const item: LibraryQuestion = {
  id: "question-1",
  internal_uuid: "question-1",
  batch_id: "batch-1",
  batch_name: "正式批次",
  course_id: "course-1",
  course_name: "医学",
  category_id: "cat-1",
  category_path: "内科/呼吸",
  source_image_id: "image-1",
  source_image_name: "原图.png",
  source_display_number: "来源第 7 题",
  question_type: "single_choice",
  stem: "肺炎的首选治疗",
  options: [{ label: "A", text: "甲" }, { label: "B", text: "乙" }],
  correct_answer: "A",
  true_label: null,
  false_label: null,
  explanation: "这是解析",
  raw_ocr_text: "原始 OCR 肺炎",
  edited_ocr_text: null,
  display_mode: "text_first",
  status: "ready",
  in_wrong_book: true,
  parts: [{ id: "part-1", source_image_id: "image-1", part_index: 0, source_y_start: 20, source_y_end: 120, crop_url: "/crop", source_url: "/source" }],
};

function response(body: unknown, ok = true): Response {
  return { ok, status: ok ? 200 : 422, json: async () => body } as Response;
}

function installFetch(currentItem: LibraryQuestion | null = item) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (url.startsWith("/api/courses")) return response(courses);
    if (url.startsWith("/api/batches") && (!init?.method || init.method === "GET")) return response(batches);
    if (url.startsWith("/api/questions?") && (!init?.method || init.method === "GET")) return response({ items: currentItem ? [currentItem] : [], total: currentItem ? 1 : 0, page: 1, page_size: 50 });
    if (init?.method === "PATCH") return response({ ...(currentItem ?? item), stem: "人工题干", category_id: "cat-2", category_path: "内科/循环", correct_answer: "B" });
    if (url.endsWith("/rerun-ocr")) return response({ ...item, stem: "重跑题干" });
    if (init?.method === "DELETE") return response({ id: url.split("/").at(-1), deleted: true, name: "正式批次", deleted_questions: 1, deleted_images: 1 });
    return response(item);
  });
}

afterEach(() => vi.restoreAllMocks());


test("loads filters search list thumbnail detail images uuid and wrong-book state", async () => {
  const fetchMock = installFetch();
  const user = userEvent.setup();
  render(<LibraryWorkspace />);

  expect(await screen.findByRole("heading", { name: "题库管理" })).toBeInTheDocument();
  expect(await screen.findByDisplayValue("肺炎的首选治疗")).toBeInTheDocument();
  expect(screen.getByText("在错题本")).toBeInTheDocument();
  expect(screen.getByText("question-1")).toBeInTheDocument();
  expect(screen.getByText("来源第 7 题")).toBeInTheDocument();
  expect(screen.getByTestId("library-question-grid")).toHaveTextContent("来源第 7 题");
  expect(screen.getByTestId("library-question-grid")).toHaveTextContent("错题本");
  expect(screen.getByAltText("题目裁切图 1")).toHaveAttribute("src", "/crop");
  expect(screen.getByAltText("来源截图 原图.png")).toHaveAttribute("src", "/source");

  await user.click(screen.getByRole("button", { name: "缩略图视图" }));
  expect(screen.getByTestId("library-question-grid")).toHaveClass("thumbnail-view");
  await user.type(screen.getByLabelText("搜索 OCR 文字"), "肺炎");
  fireEvent.change(screen.getByLabelText("课程筛选"), { target: { value: "course-1" } });
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes("search=%E8%82%BA%E7%82%8E"))).toBe(true));
  expect(fetchMock.mock.calls.some(([url]) => String(url).includes("course_id=course-1"))).toBe(true);
});


test("keeps an effective-category batch selectable after choosing its overriding category", async () => {
  const categoryBItem: LibraryQuestion = {
    ...item,
    category_id: "cat-2",
    category_path: "内科/循环",
  };
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://local.test");
    if (url.pathname === "/api/courses") return response(courses);
    if (url.pathname === "/api/batches" && (!init?.method || init.method === "GET")) {
      return response(url.searchParams.get("category_id") === "cat-2" ? batches : { items: [] });
    }
    if (url.pathname === "/api/questions" && (!init?.method || init.method === "GET")) {
      return response({ items: [categoryBItem], total: 1, page: 1, page_size: 50 });
    }
    return response(categoryBItem);
  });
  render(<LibraryWorkspace />);

  await screen.findByDisplayValue(categoryBItem.stem);
  fireEvent.change(screen.getByLabelText("课程筛选"), { target: { value: "course-1" } });
  fireEvent.change(screen.getByLabelText("分类筛选"), { target: { value: "cat-2" } });
  const batchOption = await screen.findByRole("option", { name: "正式批次（1）" });
  expect(batchOption).toHaveValue("batch-1");
  fireEvent.change(screen.getByLabelText("批次筛选"), { target: { value: "batch-1" } });
  expect(screen.getByLabelText("批次筛选")).toHaveValue("batch-1");
  expect(fetchMock.mock.calls.some(([input]) => {
    const url = new URL(String(input), "http://local.test");
    return url.pathname === "/api/batches"
      && url.searchParams.get("course_id") === "course-1"
      && url.searchParams.get("category_id") === "cat-2";
  })).toBe(true);
});


test("edits fields category options answer and saves real payload", async () => {
  const fetchMock = installFetch();
  const user = userEvent.setup();
  render(<LibraryWorkspace />);
  await screen.findByDisplayValue("肺炎的首选治疗");

  await user.clear(screen.getByLabelText("编辑题干"));
  await user.type(screen.getByLabelText("编辑题干"), "人工题干");
  fireEvent.change(screen.getByLabelText("题目分类"), { target: { value: "cat-2" } });
  await user.clear(screen.getByLabelText("选项 A 文本"));
  await user.type(screen.getByLabelText("选项 A 文本"), "新甲");
  fireEvent.change(screen.getByLabelText("正确答案"), { target: { value: "B" } });
  fireEvent.change(screen.getByLabelText("显示模式"), { target: { value: "image_first" } });
  await user.click(screen.getByRole("button", { name: "保存题目" }));

  await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(true));
  const request = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH")?.[1] as RequestInit;
  expect(JSON.parse(request.body as string)).toMatchObject({
    category_id: "cat-2",
    stem: "人工题干",
    options: [{ label: "A", text: "新甲" }, { label: "B", text: "乙" }],
    correct_answer: "B",
    display_mode: "image_first",
  });
});


test("normalizes a single-choice answer and mappings when switching to true-false", async () => {
  const fetchMock = installFetch();
  render(<LibraryWorkspace />);
  await screen.findByDisplayValue(item.stem);

  fireEvent.change(screen.getByRole("combobox", { name: /^题型$/ }), { target: { value: "true_false" } });
  fireEvent.click(screen.getByRole("button", { name: "保存题目" }));

  await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(true));
  const request = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH")?.[1] as RequestInit;
  expect(JSON.parse(request.body as string)).toMatchObject({
    question_type: "true_false",
    correct_answer: true,
    true_label: "A",
    false_label: "B",
  });
});


test("converts a true-false boolean to its mapped option when switching to single-choice", async () => {
  const trueFalseItem: LibraryQuestion = {
    ...item,
    question_type: "true_false",
    correct_answer: false,
    true_label: "A",
    false_label: "B",
  };
  const fetchMock = installFetch(trueFalseItem);
  render(<LibraryWorkspace />);
  await screen.findByDisplayValue(item.stem);

  fireEvent.change(screen.getByRole("combobox", { name: /^题型$/ }), { target: { value: "single_choice" } });
  fireEvent.click(screen.getByRole("button", { name: "保存题目" }));

  await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(true));
  const request = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH")?.[1] as RequestInit;
  expect(JSON.parse(request.body as string)).toMatchObject({
    question_type: "single_choice",
    correct_answer: "B",
  });
});


test("confirms rerun question delete and batch delete before real requests", async () => {
  const fetchMock = installFetch();
  const confirmMock = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValue(true);
  render(<LibraryWorkspace />);
  await screen.findByDisplayValue("肺炎的首选治疗");

  fireEvent.click(screen.getByRole("button", { name: "删除单题" }));
  expect(fetchMock.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "单题重新 OCR" }));
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/rerun-ocr"))).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: "删除单题" }));
  await waitFor(() => expect(fetchMock.mock.calls.some(([url, init]) => String(url) === "/api/questions/question-1" && init?.method === "DELETE")).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: "删除当前批次" }));
  await waitFor(() => expect(fetchMock.mock.calls.some(([url, init]) => String(url) === "/api/batches/batch-1" && init?.method === "DELETE")).toBe(true));
  expect(confirmMock).toHaveBeenCalledTimes(4);
});


test("deletes a selected batch even when it has no ready questions", async () => {
  const emptyBatches = {
    items: [{ ...batches.items[0], question_count: 0 }],
  };
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (url.startsWith("/api/courses")) return response(courses);
    if (url.startsWith("/api/batches") && (!init?.method || init.method === "GET")) return response(emptyBatches);
    if (url.startsWith("/api/questions?") && (!init?.method || init.method === "GET")) return response({ items: [], total: 0, page: 1, page_size: 50 });
    if (url === "/api/batches/batch-1" && init?.method === "DELETE") return response({ id: "batch-1", deleted: true, name: "正式批次", deleted_questions: 0, deleted_images: 0 });
    return response(item);
  });
  vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValue(true);
  render(<LibraryWorkspace />);
  await screen.findByText("当前筛选条件下没有正式题目。");

  fireEvent.change(screen.getByLabelText("批次筛选"), { target: { value: "batch-1" } });
  const removeButton = await screen.findByRole("button", { name: "删除筛选批次" });
  fireEvent.click(removeButton);
  expect(fetchMock.mock.calls.some(([url, init]) => String(url) === "/api/batches/batch-1" && init?.method === "DELETE")).toBe(false);
  fireEvent.click(removeButton);

  await waitFor(() => expect(fetchMock.mock.calls.some(([url, init]) => String(url) === "/api/batches/batch-1" && init?.method === "DELETE")).toBe(true));
});


test("refreshes the formal library when rerun moves a question to review", async () => {
  let rerunAttempted = false;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (url.startsWith("/api/courses")) return response(courses);
    if (url.startsWith("/api/batches")) return response(batches);
    if (url.startsWith("/api/questions?") && (!init?.method || init.method === "GET")) {
      return response({ items: rerunAttempted ? [] : [item], total: rerunAttempted ? 0 : 1, page: 1, page_size: 50 });
    }
    if (url.endsWith("/rerun-ocr")) {
      rerunAttempted = true;
      return response({ detail: "重新 OCR 后题目需要人工检查" }, false);
    }
    return response(item);
  });
  vi.spyOn(window, "confirm").mockReturnValue(true);
  render(<LibraryWorkspace />);
  await screen.findByDisplayValue(item.stem);

  fireEvent.click(screen.getByRole("button", { name: /OCR/ }));

  expect(await screen.findByRole("alert")).toHaveTextContent("重新 OCR 后题目需要人工检查");
  await waitFor(() => expect(document.querySelector(".library-empty")).not.toBeNull());
});


test("loads the 51st question through server pagination", async () => {
  const firstPage = Array.from({ length: 50 }, (_, index) => ({
    ...item,
    id: `question-${index + 1}`,
    internal_uuid: `question-${index + 1}`,
    stem: `分页题目 ${index + 1}`,
  }));
  const secondPageItem = {
    ...item,
    id: "question-51",
    internal_uuid: "question-51",
    stem: "分页题目 51",
  };
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = new URL(String(input), "http://local.test");
    if (url.pathname === "/api/courses") return response(courses);
    if (url.pathname === "/api/batches") return response(batches);
    if (url.pathname === "/api/questions") {
      const page = Number(url.searchParams.get("page"));
      return response({
        items: page === 2 ? [secondPageItem] : firstPage,
        total: 51,
        page,
        page_size: 50,
      });
    }
    return response(item);
  });

  render(<LibraryWorkspace />);
  expect(await screen.findByDisplayValue("分页题目 1")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
  expect(screen.getByText("第 1 页，共 2 页")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "下一页" }));

  expect(await screen.findByDisplayValue("分页题目 51")).toBeInTheDocument();
  expect(screen.getByText("第 2 页，共 2 页")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
  expect(fetchMock.mock.calls.some(([url]) => String(url).includes("page=2"))).toBe(true);
});


test("resets server pagination to page one when filters change", async () => {
  const firstPageItem = {
    ...item,
    id: "question-1",
    internal_uuid: "question-1",
    stem: "第一页题目",
  };
  const secondPageItem = {
    ...item,
    id: "question-51",
    internal_uuid: "question-51",
    stem: "分页题目 51",
  };
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = new URL(String(input), "http://local.test");
    if (url.pathname === "/api/courses") return response(courses);
    if (url.pathname === "/api/batches") return response(batches);
    if (url.pathname === "/api/questions") {
      const requestedPage = Number(url.searchParams.get("page"));
      const filtered = url.searchParams.get("question_type") === "true_false";
      return response({
        items: filtered
          ? [firstPageItem]
          : requestedPage === 2
            ? [secondPageItem]
            : [firstPageItem],
        total: filtered ? 1 : 51,
        page: requestedPage,
        page_size: 50,
      });
    }
    return response(item);
  });
  render(<LibraryWorkspace />);
  await screen.findByText("第 1 页，共 2 页");
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  await screen.findByDisplayValue("分页题目 51");

  fireEvent.change(screen.getByLabelText("题型筛选"), {
    target: { value: "true_false" },
  });

  await waitFor(() => {
    const questionUrls = fetchMock.mock.calls
      .map(([url]) => String(url))
      .filter((url) => url.startsWith("/api/questions?"));
    expect(questionUrls.at(-1)).toContain("page=1");
    expect(questionUrls.at(-1)).toContain("question_type=true_false");
  });
  expect(screen.getByText("第 1 页，共 1 页")).toBeInTheDocument();
});


test("returns to the last valid page after deleting the only item on the final page", async () => {
  let deleted = false;
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://local.test");
    if (url.pathname === "/api/courses") return response(courses);
    if (url.pathname === "/api/batches") return response(batches);
    if (init?.method === "DELETE") {
      deleted = true;
      return response({ id: "question-51", deleted: true });
    }
    if (url.pathname === "/api/questions") {
      const requestedPage = Number(url.searchParams.get("page"));
      if (requestedPage === 2) {
        return response({
          items: deleted ? [] : [{
            ...item,
            id: "question-51",
            internal_uuid: "question-51",
            stem: "末页唯一题目",
          }],
          total: deleted ? 50 : 51,
          page: 2,
          page_size: 50,
        });
      }
      return response({
        items: [{
          ...item,
          id: "question-1",
          internal_uuid: "question-1",
          stem: "第一页保留题目",
        }],
        total: deleted ? 50 : 51,
        page: 1,
        page_size: 50,
      });
    }
    return response(item);
  });

  render(<LibraryWorkspace />);
  await screen.findByDisplayValue("第一页保留题目");
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  await screen.findByDisplayValue("末页唯一题目");
  fireEvent.click(screen.getByRole("button", { name: "删除单题" }));

  expect(await screen.findByDisplayValue("第一页保留题目")).toBeInTheDocument();
  expect(screen.getByText("第 1 页，共 1 页")).toBeInTheDocument();
  expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("page=1")).length).toBeGreaterThan(1);
});


test("warns when a committed batch deletion still has internal cleanup pending", async () => {
  let deleted = false;
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (url.startsWith("/api/courses")) return response(courses);
    if (url.startsWith("/api/batches") && (!init?.method || init.method === "GET")) {
      return response(deleted ? { items: [] } : batches);
    }
    if (url.startsWith("/api/questions?") && (!init?.method || init.method === "GET")) {
      return response({
        items: deleted ? [] : [item],
        total: deleted ? 0 : 1,
        page: 1,
        page_size: 50,
      });
    }
    if (url === "/api/batches/batch-1" && init?.method === "DELETE") {
      deleted = true;
      return response({
        id: "batch-1",
        deleted: true,
        name: "正式批次",
        deleted_questions: 1,
        deleted_images: 1,
        cleanup_pending: true,
      });
    }
    return response(item);
  });
  render(<LibraryWorkspace />);
  await screen.findByDisplayValue(item.stem);

  fireEvent.click(screen.getByRole("button", { name: "删除当前批次" }));

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "内部文件清理将在下次启动继续",
  );
  await waitFor(() => expect(document.querySelector(".library-empty")).not.toBeNull());
});


test("keeps a post-delete refresh failure visible", async () => {
  let deleted = false;
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (url.startsWith("/api/courses")) return response(courses);
    if (url.startsWith("/api/batches")) return response(batches);
    if (url.startsWith("/api/questions?")) {
      return deleted
        ? response({ detail: "题目刷新失败" }, false)
        : response({ items: [item], total: 1, page: 1, page_size: 50 });
    }
    if (url === "/api/questions/question-1" && init?.method === "DELETE") {
      deleted = true;
      return response({ id: item.id, deleted: true });
    }
    return response(item);
  });
  render(<LibraryWorkspace />);
  await screen.findByDisplayValue(item.stem);

  fireEvent.click(screen.getByRole("button", { name: "删除单题" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("题目刷新失败");
});

test("keeps a batch metadata refresh failure visible after questions refresh", async () => {
  let deleted = false;
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (url.startsWith("/api/courses")) return response(courses);
    if (url === "/api/batches/batch-1" && init?.method === "DELETE") {
      deleted = true;
      return response({
        id: "batch-1",
        deleted: true,
        name: "正式批次",
        deleted_questions: 1,
        deleted_images: 1,
        cleanup_pending: false,
      });
    }
    if (url.startsWith("/api/batches")) {
      return deleted
        ? response({ detail: "批次刷新失败" }, false)
        : response(batches);
    }
    if (url.startsWith("/api/questions?")) {
      return response({
        items: deleted ? [] : [item],
        total: deleted ? 0 : 1,
        page: 1,
        page_size: 50,
      });
    }
    return response(item);
  });
  render(<LibraryWorkspace />);
  await screen.findByDisplayValue(item.stem);

  fireEvent.click(screen.getByRole("button", { name: "删除当前批次" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("批次刷新失败");
});

test("keeps a metadata failure after a later question-only recovery", async () => {
  let deleted = false;
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://local.test");
    if (url.pathname === "/api/courses") return response(courses);
    if (url.pathname === "/api/batches/batch-1" && init?.method === "DELETE") {
      deleted = true;
      return response({
        id: "batch-1",
        deleted: true,
        name: "正式批次",
        deleted_questions: 1,
        deleted_images: 1,
        cleanup_pending: false,
      });
    }
    if (url.pathname === "/api/batches") {
      return deleted
        ? response({ detail: "批次刷新失败" }, false)
        : response(batches);
    }
    if (url.pathname === "/api/questions") {
      if (!deleted) {
        return response({ items: [item], total: 1, page: 1, page_size: 50 });
      }
      if (url.searchParams.get("question_type") === "true_false") {
        return response({ items: [], total: 0, page: 1, page_size: 50 });
      }
      return response({ detail: "题目刷新失败" }, false);
    }
    return response(item);
  });
  render(<LibraryWorkspace />);
  await screen.findByDisplayValue(item.stem);

  fireEvent.click(screen.getByRole("button", { name: "删除当前批次" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("题目刷新失败");

  fireEvent.change(screen.getByLabelText("题型筛选"), {
    target: { value: "true_false" },
  });

  await waitFor(() => {
    expect(screen.getByRole("alert")).toHaveTextContent("批次刷新失败");
    expect(screen.getByRole("alert")).not.toHaveTextContent("题目刷新失败");
  });
});


test("ignores an older question filter response that finishes last", async () => {
  let resolveOld: ((value: Response) => void) | undefined;
  let oldStarted = false;
  const oldResponse = new Promise<Response>((resolve) => {
    resolveOld = resolve;
  });
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = new URL(String(input), "http://local.test");
    if (url.pathname === "/api/courses") return response(courses);
    if (url.pathname === "/api/batches") return response(batches);
    if (url.searchParams.get("question_type") === "true_false") {
      oldStarted = true;
      return oldResponse;
    }
    if (url.searchParams.get("question_type") === "single_choice") {
      return response({
        items: [{ ...item, stem: "最新单选结果" }],
        total: 1,
        page: 1,
        page_size: 50,
      });
    }
    return response({ items: [item], total: 1, page: 1, page_size: 50 });
  });
  render(<LibraryWorkspace />);
  await screen.findByDisplayValue(item.stem);
  const filter = screen.getByLabelText("题型筛选");
  fireEvent.change(filter, { target: { value: "true_false" } });
  await waitFor(() => expect(oldStarted).toBe(true));
  fireEvent.change(filter, { target: { value: "single_choice" } });
  await screen.findByDisplayValue("最新单选结果");

  await act(async () => {
    resolveOld?.(response({
      items: [{
        ...item,
        question_type: "true_false",
        stem: "过期判断结果",
      }],
      total: 1,
      page: 1,
      page_size: 50,
    }));
    await Promise.resolve();
  });

  expect(screen.queryByDisplayValue("过期判断结果")).not.toBeInTheDocument();
  expect(screen.getByDisplayValue("最新单选结果")).toBeInTheDocument();
});


test("ignores an older batch metadata response that finishes last", async () => {
  let resolveOld: ((value: Response) => void) | undefined;
  let oldStarted = false;
  const oldResponse = new Promise<Response>((resolve) => {
    resolveOld = resolve;
  });
  const latestBatch = {
    ...batches.items[0],
    id: "batch-2",
    name: "最新批次",
    category_id: "cat-2",
    category_path: "内科/循环",
  };
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = new URL(String(input), "http://local.test");
    if (url.pathname === "/api/courses") return response(courses);
    if (url.pathname === "/api/questions") {
      return response({ items: [item], total: 1, page: 1, page_size: 50 });
    }
    if (url.searchParams.get("category_id") === "cat-1") {
      oldStarted = true;
      return oldResponse;
    }
    if (url.searchParams.get("category_id") === "cat-2") {
      return response({ items: [latestBatch] });
    }
    return response(batches);
  });
  render(<LibraryWorkspace />);
  await screen.findByDisplayValue(item.stem);
  const filter = screen.getByLabelText("分类筛选");
  fireEvent.change(filter, { target: { value: "cat-1" } });
  await waitFor(() => expect(oldStarted).toBe(true));
  fireEvent.change(filter, { target: { value: "cat-2" } });
  expect(
    await screen.findByRole("option", { name: /最新批次/ }),
  ).toBeInTheDocument();

  await act(async () => {
    resolveOld?.(response(batches));
    await Promise.resolve();
  });

  expect(
    screen.getByRole("option", { name: /最新批次/ }),
  ).toBeInTheDocument();
});
