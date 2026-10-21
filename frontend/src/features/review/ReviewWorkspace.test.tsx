import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { ReviewWorkspace } from "./ReviewWorkspace";


const item = {
  id: "candidate-1",
  batch_id: "job-1",
  batch_name: "异常批次",
  course_name: "医学",
  category_path: "内科/呼吸",
  source_image_id: "image-1",
  source_image_name: "异常.png",
  source_display_number: "1",
  question_type: "single_choice",
  stem: "旧题干",
  options: [{ label: "A", text: "甲" }, { label: "B", text: "乙" }],
  correct_answer: null,
  true_label: null,
  false_label: null,
  explanation: null,
  raw_ocr_text: "原始 OCR",
  edited_ocr_text: null,
  ocr_confidence: 0.72,
  parse_confidence: 0.61,
  display_mode: "text",
  anomaly_flags: [{ code: "MISSING_ANSWER", severity: "error", message: "未识别到正确答案" }],
  status: "review",
  duplicate_of_id: null,
  parts: [{ id: "part-1", source_image_id: "image-1", part_index: 0, source_y_start: 20, source_y_end: 120, source_height: 200, crop_url: "/crop", source_url: "/source" }],
  relationships: [{ id: "relationship-1", batch_id: "job-1", batch_name: "异常批次", course_name: "医学", category_path: "内科/呼吸", relation_type: "cross_screenshot", primary_candidate_id: "candidate-1", secondary_candidate_id: "candidate-2", decision: "suggested", confidence: 0.9, evidence: { merged_text_preview: "合并预览" }, status: "pending" }],
  can_undo: true,
};

const second = { ...item, id: "candidate-2", stem: "另一题", parts: [{ ...item.parts[0], id: "part-2" }], relationships: item.relationships };
const relationshipPrimary = { ...item, source_image_name: "synthetic-A.png" };
const relationshipSecondary = { ...second, source_image_name: "synthetic-B.png" };
const bundle = { items: [item, second], relationships: item.relationships, ordering_issues: [{ id: "order-1", batch_id: "job-1", batch_name: "异常批次", course_name: "医学", category_path: "内科/呼吸", code: "NEAR_TIE", message: "截图顺序仍需检查", image_ids: ["image-1"], status: "pending" }] };

function relationshipPart(
  id: string,
  partIndex: number,
  sourceYStart: number,
  sourceYEnd: number,
) {
  return {
    ...item.parts[0],
    id,
    source_image_id: `image-${id}`,
    part_index: partIndex,
    source_y_start: sourceYStart,
    source_y_end: sourceYEnd,
    crop_url: `/crop-${id}`,
    source_url: `/source-${id}`,
  };
}

function response(body: unknown, ok = true): Response {
  return { ok, status: ok ? 200 : 422, json: async () => body } as Response;
}

const contextItem = (candidate: typeof item) => ({
  id: candidate.id,
  batch_id: candidate.batch_id,
  stem: candidate.stem,
  status: candidate.status,
});

const defaultBatchContext = {
  items: [contextItem(item), contextItem(second)],
};

function mockReviewFetch(
  handler: (url: string, init?: RequestInit) => Promise<Response> | Response,
) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (url.endsWith("/batch-context")) return response(defaultBatchContext);
    return handler(url, init);
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => { resolve = next; });
  return { promise, resolve };
}

function expectBatchActionsDisabled() {
  const duplicateAction = document.querySelector(".duplicate-controls button") as HTMLButtonElement;
  const mergeActions = Array.from(
    document.querySelectorAll(".merge-controls button"),
  ) as HTMLButtonElement[];
  expect(duplicateAction).toBeDisabled();
  expect(mergeActions).toHaveLength(3);
  mergeActions.forEach((action) => expect(action).toBeDisabled());
}


afterEach(() => vi.restoreAllMocks());


test("loads filters original crop and structured anomaly detail", async () => {
  const fetchMock = mockReviewFetch(() => response(bundle));
  render(<ReviewWorkspace jobId="job-1" />);

  expect(await screen.findByRole("heading", { name: "异常题人工检查" })).toBeInTheDocument();
  expect(screen.getAllByText("未识别到正确答案").length).toBeGreaterThan(0);
  expect(screen.getByText("截图顺序仍需检查")).toBeInTheDocument();
  expect(screen.getByAltText("原始截图 异常.png")).toHaveAttribute("src", "/source");
  expect(screen.getByText("OCR 72%")).toBeInTheDocument();
  expect(screen.getByLabelText("课程筛选")).toHaveValue("医学");
  expect(screen.getByRole("option", { name: "答案冲突" })).toBeInTheDocument();
  expect(screen.getByRole("option", { name: "判断题映射失败" })).toBeInTheDocument();
  expect(screen.getByRole("option", { name: "不支持的题型" })).toBeInTheDocument();
  expect(screen.getByRole("option", { name: "图片损坏" })).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("题型筛选"), { target: { value: "single_choice" } });
  await waitFor(() => expect(fetchMock.mock.calls.at(-1)?.[0]).toContain("question_type=single_choice"));
});


test("edits OCR fields options answer crop and saves real payload", async () => {
  const fetchMock = mockReviewFetch((url, init) => {
    if (url === "/api/review-items/candidate-1" && init?.method === "PATCH") {
      return response({ ...item, stem: "新题干", correct_answer: "C" });
    }
    return response(bundle);
  });
  const user = userEvent.setup();
  render(<ReviewWorkspace jobId="job-1" />);
  await screen.findByDisplayValue("旧题干");

  await user.clear(screen.getByLabelText("编辑后的题干"));
  await user.type(screen.getByLabelText("编辑后的题干"), "新题干");
  await user.type(screen.getByLabelText("编辑 OCR 文本"), "人工 OCR");
  await user.click(screen.getByRole("button", { name: "新增选项" }));
  expect(screen.getByLabelText("选项 C 字母")).toHaveValue("C");
  await user.type(screen.getByLabelText("选项 C 文本"), "丙");
  fireEvent.change(screen.getByLabelText("正确答案"), { target: { value: "C" } });
  fireEvent.change(screen.getByLabelText("裁切上边界"), { target: { value: "30" } });
  fireEvent.change(screen.getByLabelText("裁切下边界"), { target: { value: "150" } });
  await user.click(screen.getByRole("button", { name: "保存修改" }));

  const saveCall = await waitFor(() => {
    const next = fetchMock.mock.calls.find(([url, init]) =>
      String(url) === "/api/review-items/candidate-1" && init?.method === "PATCH"
    );
    expect(next).toBeDefined();
    return next;
  });
  expect(JSON.parse(String((saveCall?.[1] as RequestInit).body))).toMatchObject({
    stem: "新题干",
    edited_ocr_text: "人工 OCR",
    correct_answer: "C",
    parts: [{ id: "part-1", part_index: 0, source_y_start: 30, source_y_end: 150 }],
  });
});


test("confirms destructive actions relation merge reruns and undo", async () => {
  const fetchMock = mockReviewFetch(() => response(bundle));
  const confirmMock = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValue(true);
  render(<ReviewWorkspace jobId="job-1" />);
  await screen.findByDisplayValue("旧题干");

  fireEvent.click(screen.getByRole("button", { name: "删除候选" }));
  expect(fetchMock.mock.calls.some(([url, init]) =>
    String(url) === "/api/review-items/candidate-1" && init?.method === "DELETE"
  )).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "忽略候选" }));
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/ignore"))).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: "确认跨截图合并" }));
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes("review-items/candidate-1/merge"))).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: "重新运行单题 OCR" }));
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes("rerun-ocr"))).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: "撤销最近一次修改" }));

  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/undo"))).toBe(true));
  expect(confirmMock).toHaveBeenCalled();
});


test("normalizes single choice into a valid true false save payload", async () => {
  const answered = { ...item, correct_answer: "B" };
  const fetchMock = mockReviewFetch((url, init) => {
    if (url === "/api/review-items/candidate-1" && init?.method === "PATCH") {
      return response({ ...answered, question_type: "true_false", correct_answer: false, true_label: "A", false_label: "B" });
    }
    return response({ ...bundle, items: [answered, second] });
  });
  render(<ReviewWorkspace jobId="job-1" />);

  fireEvent.change(await screen.findByRole("combobox", { name: "题型" }), { target: { value: "true_false" } });
  expect(screen.getByLabelText("判断答案")).toHaveValue("false");
  expect(screen.getByLabelText("正确选项映射")).toHaveValue("A");
  expect(screen.getByLabelText("错误选项映射")).toHaveValue("B");
  fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

  const saveCall = await waitFor(() => {
    const next = fetchMock.mock.calls.find(([url, init]) =>
      String(url) === "/api/review-items/candidate-1" && init?.method === "PATCH"
    );
    expect(next).toBeDefined();
    return next;
  });
  const payload = JSON.parse(String((saveCall?.[1] as RequestInit).body));
  expect(payload).toMatchObject({
    question_type: "true_false",
    correct_answer: false,
    true_label: "A",
    false_label: "B",
  });
});


test("normalizes true false into a valid single choice save payload", async () => {
  const judgment = {
    ...item,
    question_type: "true_false",
    correct_answer: false,
    true_label: "A",
    false_label: "B",
  };
  const fetchMock = mockReviewFetch((url, init) => {
    if (url === "/api/review-items/candidate-1" && init?.method === "PATCH") {
      return response({ ...judgment, question_type: "single_choice", correct_answer: "B" });
    }
    return response({ ...bundle, items: [judgment, second] });
  });
  render(<ReviewWorkspace jobId="job-1" />);

  fireEvent.change(await screen.findByRole("combobox", { name: "题型" }), { target: { value: "single_choice" } });
  expect(screen.getByLabelText("正确答案")).toHaveValue("B");
  fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

  const saveCall = await waitFor(() => {
    const next = fetchMock.mock.calls.find(([url, init]) =>
      String(url) === "/api/review-items/candidate-1" && init?.method === "PATCH"
    );
    expect(next).toBeDefined();
    return next;
  });
  const payload = JSON.parse(String((saveCall?.[1] as RequestInit).body));
  expect(payload).toMatchObject({ question_type: "single_choice", correct_answer: "B" });
});


test("uses required delete and direct merge item APIs", async () => {
  const fetchMock = mockReviewFetch(() => response(bundle));
  vi.spyOn(window, "confirm").mockReturnValue(true);
  render(<ReviewWorkspace jobId="job-1" />);
  await screen.findByDisplayValue("旧题干");

  fireEvent.click(screen.getByRole("button", { name: "删除候选" }));
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url) === "/api/review-items/candidate-1")).toBe(true));
  const deleteCall = fetchMock.mock.calls.find(([url]) => String(url) === "/api/review-items/candidate-1");
  expect(deleteCall?.[1]).toMatchObject({ method: "DELETE" });
  expect(JSON.parse(String((deleteCall?.[1] as RequestInit).body))).toEqual({ confirmed: true });

  fireEvent.click(screen.getByRole("button", { name: "合并下一题" }));
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url) === "/api/review-items/candidate-1/merge")).toBe(true));
  const mergeCall = fetchMock.mock.calls.find(([url]) => String(url) === "/api/review-items/candidate-1/merge");
  expect(JSON.parse(String((mergeCall?.[1] as RequestInit).body))).toMatchObject({
    secondary_candidate_id: "candidate-2",
    confirmed: true,
  });
});


test("uses unfiltered source order and exposes a hidden ready merge target", async () => {
  const hiddenReady = { ...second, id: "candidate-hidden", stem: "hidden ready neighbor", status: "ready" };
  const visibleLast = { ...second, id: "candidate-last", stem: "visible final item" };
  const filteredBundle = { ...bundle, items: [item, visibleLast], relationships: [] };
  const batchContext = { items: [contextItem(item), contextItem(hiddenReady), contextItem(visibleLast)] };
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (url === "/api/review-items/candidate-1/batch-context") return response(batchContext);
    if (url.startsWith("/api/review-items?")) return response(filteredBundle);
    if (url === "/api/review-items/candidate-1/merge") return response(item);
    throw new Error(`unexpected fetch: ${url}`);
  });
  vi.spyOn(window, "confirm").mockReturnValue(true);
  render(<ReviewWorkspace jobId="job-1" />);

  expect(await screen.findAllByRole("option", { name: "hidden ready neighbor" })).toHaveLength(2);
  const mergeControls = document.querySelector(".merge-controls");
  const buttons = mergeControls?.querySelectorAll("button");
  expect(buttons).toHaveLength(3);
  fireEvent.click(buttons?.[2] as HTMLButtonElement);

  const mergeCall = await waitFor(() => {
    const next = fetchMock.mock.calls.find(([url]) => String(url) === "/api/review-items/candidate-1/merge");
    expect(next).toBeDefined();
    return next;
  });
  expect(JSON.parse(String((mergeCall?.[1] as RequestInit).body))).toMatchObject({
    secondary_candidate_id: "candidate-hidden",
    confirmed: true,
  });
});


test("uses the hidden middle candidate as the previous item", async () => {
  const hiddenReady = { ...second, id: "candidate-hidden", stem: "hidden ready neighbor", status: "ready" };
  const visibleLast = { ...second, id: "candidate-last", stem: "visible final item" };
  const filteredBundle = { ...bundle, items: [item, visibleLast], relationships: [] };
  const batchContext = { items: [contextItem(item), contextItem(hiddenReady), contextItem(visibleLast)] };
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (url.endsWith("/batch-context")) return response(batchContext);
    if (url.startsWith("/api/review-items?")) return response(filteredBundle);
    if (url === "/api/review-items/candidate-hidden/merge") return response(hiddenReady);
    throw new Error(`unexpected fetch: ${url}`);
  });
  vi.spyOn(window, "confirm").mockReturnValue(true);
  render(<ReviewWorkspace jobId="job-1" />);
  await screen.findByDisplayValue(item.stem);

  fireEvent.click(screen.getByRole("button", { name: /visible final item/ }));
  const mergeControls = await waitFor(() => {
    const next = document.querySelector(".merge-controls");
    expect(next?.querySelectorAll("button")[1]).toBeEnabled();
    return next;
  });
  fireEvent.click(mergeControls?.querySelectorAll("button")[1] as HTMLButtonElement);

  const mergeCall = await waitFor(() => {
    const next = fetchMock.mock.calls.find(([url]) => String(url) === "/api/review-items/candidate-hidden/merge");
    expect(next).toBeDefined();
    return next;
  });
  expect(JSON.parse(String((mergeCall?.[1] as RequestInit).body))).toMatchObject({
    secondary_candidate_id: "candidate-last",
    confirmed: true,
  });
});


test("disables batch actions when batch context fails instead of falling back", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (url.endsWith("/batch-context")) return response({ detail: "batch context unavailable" }, false);
    if (url.startsWith("/api/review-items?")) return response(bundle);
    throw new Error(`unexpected fetch: ${url}`);
  });
  render(<ReviewWorkspace jobId="job-1" />);

  expect(await screen.findByRole("alert")).toHaveTextContent("batch context unavailable");
  const duplicateAction = document.querySelector(".duplicate-controls button");
  const mergeButtons = document.querySelectorAll(".merge-controls button");
  expect(duplicateAction).toBeDisabled();
  expect(mergeButtons[0]).toBeDisabled();
  expect(mergeButtons[2]).toBeDisabled();
});


test("disables all batch actions while batch context is loading", async () => {
  const pendingContext = deferred<Response>();
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (url === "/api/review-items/candidate-1/batch-context") return pendingContext.promise;
    if (url.startsWith("/api/review-items?")) return response(bundle);
    throw new Error(`unexpected fetch: ${url}`);
  });
  render(<ReviewWorkspace jobId="job-1" />);

  await screen.findByDisplayValue(item.stem);
  await waitFor(() => expect(fetchMock.mock.calls.some(
    ([url]) => String(url) === "/api/review-items/candidate-1/batch-context",
  )).toBe(true));
  expectBatchActionsDisabled();
});


test("disables all batch actions when context omits the active candidate", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (url === "/api/review-items/candidate-1/batch-context") {
      return response({ items: [contextItem(second)] });
    }
    if (url.startsWith("/api/review-items?")) return response(bundle);
    throw new Error(`unexpected fetch: ${url}`);
  });
  render(<ReviewWorkspace jobId="job-1" />);

  await screen.findAllByRole("option", { name: second.stem });
  expectBatchActionsDisabled();
});


test("clears a failed batch context alert after a successful retry", async () => {
  const retryTarget = { ...second, id: "candidate-retry", stem: "retry target" };
  const retryBundle = { ...bundle, items: [item, retryTarget], relationships: [] };
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (url.startsWith("/api/review-items?")) return response(retryBundle);
    if (url === "/api/review-items/candidate-1/batch-context") {
      return response({ detail: "batch context unavailable" }, false);
    }
    if (url === "/api/review-items/candidate-retry/batch-context") {
      return response({ items: [contextItem(item), contextItem(retryTarget)] });
    }
    throw new Error(`unexpected fetch: ${url}`);
  });
  render(<ReviewWorkspace jobId="job-1" />);

  expect(await screen.findByRole("alert")).toHaveTextContent("batch context unavailable");
  fireEvent.click(screen.getByRole("button", { name: /retry target/ }));

  await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
});


test("keeps a newer save failure when an older batch context request succeeds", async () => {
  const pendingContext = deferred<Response>();
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (url === "/api/review-items/candidate-1/batch-context") return pendingContext.promise;
    if (url === "/api/review-items/candidate-1" && init?.method === "PATCH") {
      return response({ detail: "save failed after context started" }, false);
    }
    if (url.startsWith("/api/review-items?")) return response(bundle);
    throw new Error(`unexpected fetch: ${url}`);
  });
  render(<ReviewWorkspace jobId="job-1" />);

  fireEvent.click(await screen.findByRole("button", { name: "保存修改" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("save failed after context started");

  pendingContext.resolve(response(defaultBatchContext));
  expect(await screen.findAllByRole("option", { name: second.stem })).toHaveLength(2);
  expect(screen.getByRole("alert")).toHaveTextContent("save failed after context started");
});


test("ignores a stale batch context response after selecting another item", async () => {
  const visibleLast = { ...second, id: "candidate-last", stem: "current selection" };
  const nextForLast = { ...second, id: "candidate-next", stem: "current selection next" };
  const filteredBundle = { ...bundle, items: [item, visibleLast], relationships: [] };
  const firstContext = deferred<Response>();
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (url.startsWith("/api/review-items?")) return response(filteredBundle);
    if (url === "/api/review-items/candidate-1/batch-context") return firstContext.promise;
    if (url === "/api/review-items/candidate-last/batch-context") {
      return response({ items: [contextItem(visibleLast), contextItem(nextForLast)] });
    }
    throw new Error(`unexpected fetch: ${url}`);
  });
  render(<ReviewWorkspace jobId="job-1" />);
  await waitFor(() => expect(fetchMock.mock.calls.some(
    ([url]) => String(url) === "/api/review-items/candidate-1/batch-context",
  )).toBe(true));

  fireEvent.click(screen.getByRole("button", { name: /current selection/ }));
  expect(await screen.findAllByRole("option", { name: "current selection next" })).toHaveLength(2);

  firstContext.resolve(response({ items: [contextItem(item), contextItem(second)] }));
  await waitFor(() => {
    expect(screen.getAllByRole("option", { name: "current selection next" })).toHaveLength(2);
    expect(screen.queryAllByRole("option", { name: second.stem })).toHaveLength(0);
  });
  expect(document.querySelectorAll(".merge-controls button")[2]).toBeEnabled();
});


test.each([
  {
    button: "保留截图 A（synthetic-A.png），忽略截图 B（synthetic-B.png）",
    confirmation: "确认保留截图 A（synthetic-A.png）并忽略截图 B（synthetic-B.png）？",
    decision: "only_primary",
  },
  {
    button: "保留截图 B（synthetic-B.png），忽略截图 A（synthetic-A.png）",
    confirmation: "确认保留截图 B（synthetic-B.png）并忽略截图 A（synthetic-A.png）？",
    decision: "only_secondary",
  },
])("maps inline retention action $decision to the named screenshots", async ({ button, confirmation, decision }) => {
  const relationship = item.relationships[0];
  const inlineBundle = {
    ...bundle,
    items: [relationshipPrimary, relationshipSecondary],
    relationships: [relationship],
  };
  const fetchMock = mockReviewFetch(() => response(inlineBundle));
  const confirmMock = vi.spyOn(window, "confirm").mockReturnValue(true);
  render(<ReviewWorkspace jobId="job-1" />);

  fireEvent.click(await screen.findByRole("button", { name: button }));

  const call = await waitFor(() => {
    const found = fetchMock.mock.calls.find(([url, init]) => (
      String(url) === "/api/review-relationships/relationship-1/resolve"
      && init?.method === "POST"
    ));
    expect(found).toBeDefined();
    return found;
  });
  expect(confirmMock).toHaveBeenCalledWith(confirmation);
  expect(JSON.parse(String((call?.[1] as RequestInit).body))).toEqual({
    decision,
    confirmed: true,
  });
});

test.each([
  {
    button: "保留截图 A（synthetic-A.png），忽略截图 B（synthetic-B.png）",
    confirmation: "确认保留截图 A（synthetic-A.png）并忽略截图 B（synthetic-B.png）？",
    decision: "only_primary",
  },
  {
    button: "保留截图 B（synthetic-B.png），忽略截图 A（synthetic-A.png）",
    confirmation: "确认保留截图 B（synthetic-B.png）并忽略截图 A（synthetic-A.png）？",
    decision: "only_secondary",
  },
])("maps active relationship retention action $decision to the named screenshots", async ({ button, confirmation, decision }) => {
  const relationship = item.relationships[0];
  const detailBundle = { items: [], relationships: [relationship], ordering_issues: [] };
  const fetchMock = mockReviewFetch((url) => {
    if (url.startsWith("/api/review-items?")) return response(detailBundle);
    if (url === "/api/review-items/candidate-1") return response(relationshipPrimary);
    if (url === "/api/review-items/candidate-2") return response(relationshipSecondary);
    if (url === "/api/review-relationships/relationship-1/resolve") return response(detailBundle);
    throw new Error(`unexpected fetch: ${url}`);
  });
  const confirmMock = vi.spyOn(window, "confirm").mockReturnValue(true);
  render(<ReviewWorkspace jobId="job-1" />);

  fireEvent.click(await screen.findByRole("button", { name: button }));

  const call = await waitFor(() => {
    const found = fetchMock.mock.calls.find(([url, init]) => (
      String(url) === "/api/review-relationships/relationship-1/resolve"
      && init?.method === "POST"
    ));
    expect(found).toBeDefined();
    return found;
  });
  expect(confirmMock).toHaveBeenCalledWith(confirmation);
  expect(JSON.parse(String((call?.[1] as RequestInit).body))).toEqual({
    decision,
    confirmed: true,
  });
});

test("falls back to screenshot side labels when active relationship names are missing", async () => {
  const relationship = item.relationships[0];
  const detailBundle = { items: [], relationships: [relationship], ordering_issues: [] };
  const missingNamePrimary = { ...relationshipPrimary, source_image_name: "" };
  const missingNameSecondary = { ...relationshipSecondary, source_image_name: "" };
  mockReviewFetch((url) => {
    if (url.startsWith("/api/review-items?")) return response(detailBundle);
    if (url === "/api/review-items/candidate-1") return response(missingNamePrimary);
    if (url === "/api/review-items/candidate-2") return response(missingNameSecondary);
    throw new Error(`unexpected fetch: ${url}`);
  });
  render(<ReviewWorkspace jobId="job-1" />);

  expect(await screen.findByRole("button", { name: "保留截图 A，忽略截图 B" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "保留截图 B，忽略截图 A" })).toBeInTheDocument();
});

test("navigates relationship parts independently on both sides", async () => {
  const relationship = item.relationships[0];
  const primary = {
    ...item,
    parts: [
      relationshipPart("a-1", 0, 10, 100),
      relationshipPart("a-2", 1, 30, 150),
    ],
  };
  const secondary = {
    ...second,
    parts: [
      relationshipPart("b-1", 0, 20, 120),
      relationshipPart("b-2", 1, 40, 160),
      relationshipPart("b-3", 2, 50, 170),
    ],
  };
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (url.startsWith("/api/review-items?")) {
      return response({ items: [], relationships: [relationship], ordering_issues: [] });
    }
    if (url === "/api/review-items/candidate-1") return response(primary);
    if (url === "/api/review-items/candidate-2") return response(secondary);
    throw new Error(`unexpected fetch: ${url}`);
  });
  const user = userEvent.setup();
  render(<ReviewWorkspace jobId="job-1" />);

  await user.click(await screen.findByRole("button", { name: "截图 A 片段 2" }));
  await user.click(screen.getByRole("button", { name: "截图 B 片段 3" }));

  expect(screen.getByRole("button", { name: "截图 A 片段 2" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: "截图 B 片段 3" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByAltText("截图 A：异常.png")).toHaveAttribute("src", "/source-a-2");
  expect(screen.getByAltText("截图 A 建议裁切")).toHaveAttribute("src", "/crop-a-2");
  expect(screen.getByAltText("截图 B：异常.png")).toHaveAttribute("src", "/source-b-3");
  expect(screen.getByAltText("截图 B 建议裁切")).toHaveAttribute("src", "/crop-b-3");
  expect(screen.getByLabelText("截图 A 裁切上边界")).toHaveValue("30");
  expect(screen.getByLabelText("截图 B 裁切下边界")).toHaveValue("170");
});

test("resets both relationship part selections when switching relationships", async () => {
  const firstRelationship = item.relationships[0];
  const secondRelationship = {
    ...firstRelationship,
    id: "relationship-2",
    primary_candidate_id: "candidate-3",
    secondary_candidate_id: "candidate-4",
    evidence: { merged_text_preview: "第二条关系预览" },
  };
  const candidates = {
    "candidate-1": {
      ...item,
      parts: [relationshipPart("first-a-1", 0, 10, 100), relationshipPart("first-a-2", 1, 20, 110)],
    },
    "candidate-2": {
      ...second,
      parts: [relationshipPart("first-b-1", 0, 30, 120), relationshipPart("first-b-2", 1, 40, 130)],
    },
    "candidate-3": {
      ...item,
      id: "candidate-3",
      source_image_name: "第二条-A.png",
      parts: [relationshipPart("next-a-1", 0, 50, 140), relationshipPart("next-a-2", 1, 60, 150)],
    },
    "candidate-4": {
      ...second,
      id: "candidate-4",
      source_image_name: "第二条-B.png",
      parts: [relationshipPart("next-b-1", 0, 70, 160), relationshipPart("next-b-2", 1, 80, 170)],
    },
  };
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (url.startsWith("/api/review-items?")) {
      return response({ items: [], relationships: [firstRelationship, secondRelationship], ordering_issues: [] });
    }
    const candidateId = url.match(/^\/api\/review-items\/(candidate-[1-4])$/)?.[1] as keyof typeof candidates | undefined;
    if (candidateId) return response(candidates[candidateId]);
    throw new Error(`unexpected fetch: ${url}`);
  });
  const user = userEvent.setup();
  render(<ReviewWorkspace jobId="job-1" />);

  await user.click(await screen.findByRole("button", { name: "截图 A 片段 2" }));
  await user.click(screen.getByRole("button", { name: "截图 B 片段 2" }));
  await user.click(screen.getByRole("button", { name: /第二条关系预览/ }));

  expect(await screen.findByAltText("截图 A：第二条-A.png")).toHaveAttribute("src", "/source-next-a-1");
  expect(screen.getByAltText("截图 B：第二条-B.png")).toHaveAttribute("src", "/source-next-b-1");
  expect(screen.getByRole("button", { name: "截图 A 片段 1" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: "截图 B 片段 1" })).toHaveAttribute("aria-pressed", "true");
});

test("hides stale relationship parts while the next relationship loads", async () => {
  const firstRelationship = item.relationships[0];
  const secondRelationship = {
    ...firstRelationship,
    id: "relationship-2",
    primary_candidate_id: "candidate-3",
    secondary_candidate_id: "candidate-4",
    evidence: { merged_text_preview: "延迟加载的第二条关系" },
  };
  const firstPrimary = {
    ...item,
    source_image_name: "第一条-A.png",
    parts: [relationshipPart("first-a-1", 0, 10, 100), relationshipPart("first-a-2", 1, 20, 110)],
  };
  const firstSecondary = {
    ...second,
    source_image_name: "第一条-B.png",
    parts: [relationshipPart("first-b-1", 0, 30, 120), relationshipPart("first-b-2", 1, 40, 130)],
  };
  const nextPrimary = {
    ...item,
    id: "candidate-3",
    source_image_name: "第二条-A.png",
    parts: [relationshipPart("next-a-1", 0, 50, 140), relationshipPart("next-a-2", 1, 60, 150)],
  };
  const nextSecondary = {
    ...second,
    id: "candidate-4",
    source_image_name: "第二条-B.png",
    parts: [relationshipPart("next-b-1", 0, 70, 160), relationshipPart("next-b-2", 1, 80, 170)],
  };
  const nextPrimaryResponse = deferred<Response>();
  const nextSecondaryResponse = deferred<Response>();
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (url.startsWith("/api/review-items?")) {
      return response({ items: [], relationships: [firstRelationship, secondRelationship], ordering_issues: [] });
    }
    if (url === "/api/review-items/candidate-1") return response(firstPrimary);
    if (url === "/api/review-items/candidate-2") return response(firstSecondary);
    if (url === "/api/review-items/candidate-3") return nextPrimaryResponse.promise;
    if (url === "/api/review-items/candidate-4") return nextSecondaryResponse.promise;
    throw new Error(`unexpected fetch: ${url}`);
  });
  const user = userEvent.setup();
  render(<ReviewWorkspace jobId="job-1" />);

  await user.click(await screen.findByRole("button", { name: "截图 A 片段 2" }));
  await user.click(screen.getByRole("button", { name: "截图 B 片段 2" }));
  expect(screen.getByAltText("截图 A：第一条-A.png")).toHaveAttribute("src", "/source-first-a-2");
  expect(screen.getByAltText("截图 B：第一条-B.png")).toHaveAttribute("src", "/source-first-b-2");

  await user.click(screen.getByRole("button", { name: /延迟加载的第二条关系/ }));

  expect(screen.queryByAltText("截图 A：第一条-A.png")).not.toBeInTheDocument();
  expect(screen.queryByAltText("截图 B：第一条-B.png")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "截图 A 片段 2" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "截图 B 片段 2" })).not.toBeInTheDocument();

  nextPrimaryResponse.resolve(response(nextPrimary));
  nextSecondaryResponse.resolve(response(nextSecondary));

  expect(await screen.findByAltText("截图 A：第二条-A.png")).toHaveAttribute("src", "/source-next-a-1");
  expect(screen.getByAltText("截图 B：第二条-B.png")).toHaveAttribute("src", "/source-next-b-1");
  expect(screen.getByRole("button", { name: "截图 A 片段 1" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: "截图 B 片段 1" })).toHaveAttribute("aria-pressed", "true");
});

test("updates selected relationship parts and saves every part", async () => {
  const relationship = item.relationships[0];
  const primary = {
    ...item,
    parts: [relationshipPart("a-1", 0, 10, 100), relationshipPart("a-2", 1, 30, 150)],
  };
  const secondary = {
    ...second,
    parts: [relationshipPart("b-1", 0, 20, 120), relationshipPart("b-2", 1, 40, 160)],
  };
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (url.startsWith("/api/review-items?")) {
      return response({ items: [], relationships: [relationship], ordering_issues: [] });
    }
    if (url === "/api/review-items/candidate-1") return response(primary);
    if (url === "/api/review-items/candidate-2") return response(secondary);
    if (url === "/api/review-relationships/relationship-1/crops" && init?.method === "PATCH") {
      return response({ primary, secondary });
    }
    throw new Error(`unexpected fetch: ${url}`);
  });
  const user = userEvent.setup();
  render(<ReviewWorkspace jobId="job-1" />);

  await user.click(await screen.findByRole("button", { name: "截图 A 片段 2" }));
  fireEvent.change(screen.getByLabelText("截图 A 裁切上边界"), { target: { value: "35" } });
  await user.click(screen.getByRole("button", { name: "截图 B 片段 2" }));
  fireEvent.change(screen.getByLabelText("截图 B 裁切下边界"), { target: { value: "155" } });
  await user.click(screen.getByRole("button", { name: "保存两侧裁切范围" }));

  const cropCall = await waitFor(() => {
    const call = fetchMock.mock.calls.find(([url, options]) => (
      String(url) === "/api/review-relationships/relationship-1/crops"
      && options?.method === "PATCH"
    ));
    expect(call).toBeDefined();
    return call;
  });
  expect(JSON.parse(String((cropCall?.[1] as RequestInit).body))).toEqual({
    primary_parts: [
      { id: "a-1", part_index: 0, source_y_start: 10, source_y_end: 100 },
      { id: "a-2", part_index: 1, source_y_start: 35, source_y_end: 150 },
    ],
    secondary_parts: [
      { id: "b-1", part_index: 0, source_y_start: 20, source_y_end: 120 },
      { id: "b-2", part_index: 1, source_y_start: 40, source_y_end: 155 },
    ],
  });
});

test("clamps relationship part selections after save", async () => {
  const relationship = item.relationships[0];
  const primary = {
    ...item,
    parts: [
      relationshipPart("a-1", 0, 10, 100),
      relationshipPart("a-2", 1, 20, 110),
      relationshipPart("a-3", 2, 30, 120),
    ],
  };
  const secondary = {
    ...second,
    parts: [relationshipPart("b-1", 0, 40, 130), relationshipPart("b-2", 1, 50, 140)],
  };
  let saveCount = 0;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (url.startsWith("/api/review-items?")) {
      return response({ items: [], relationships: [relationship], ordering_issues: [] });
    }
    if (url === "/api/review-items/candidate-1") return response(primary);
    if (url === "/api/review-items/candidate-2") return response(secondary);
    if (url === "/api/review-relationships/relationship-1/crops" && init?.method === "PATCH") {
      saveCount += 1;
      return response({
        primary: saveCount === 1 ? { ...primary, parts: primary.parts.slice(0, 2) } : primary,
        secondary,
      });
    }
    throw new Error(`unexpected fetch: ${url}`);
  });
  const user = userEvent.setup();
  render(<ReviewWorkspace jobId="job-1" />);

  await user.click(await screen.findByRole("button", { name: "截图 A 片段 3" }));
  await user.click(screen.getByRole("button", { name: "截图 B 片段 2" }));
  const saveButton = screen.getByRole("button", { name: "保存两侧裁切范围" });
  await user.click(saveButton);

  await waitFor(() => expect(screen.queryByRole("button", { name: "截图 A 片段 3" })).not.toBeInTheDocument());
  expect(screen.getByRole("button", { name: "截图 A 片段 2" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: "截图 B 片段 2" })).toHaveAttribute("aria-pressed", "true");

  await waitFor(() => expect(saveButton).toBeEnabled());
  await user.click(saveButton);
  expect(await screen.findByRole("button", { name: "截图 A 片段 3" })).toHaveAttribute("aria-pressed", "false");
  expect(screen.getByRole("button", { name: "截图 A 片段 2" })).toHaveAttribute("aria-pressed", "true");
});

test("keeps relationship edits and selection after save failure", async () => {
  const relationship = item.relationships[0];
  const primary = {
    ...item,
    parts: [relationshipPart("a-1", 0, 10, 100), relationshipPart("a-2", 1, 30, 150)],
  };
  const secondary = {
    ...second,
    parts: [relationshipPart("b-1", 0, 20, 120), relationshipPart("b-2", 1, 40, 160)],
  };
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (url.startsWith("/api/review-items?")) {
      return response({ items: [], relationships: [relationship], ordering_issues: [] });
    }
    if (url === "/api/review-items/candidate-1") return response(primary);
    if (url === "/api/review-items/candidate-2") return response(secondary);
    if (url === "/api/review-relationships/relationship-1/crops" && init?.method === "PATCH") {
      return response({ detail: "crop save failed" }, false);
    }
    throw new Error(`unexpected fetch: ${url}`);
  });
  const user = userEvent.setup();
  render(<ReviewWorkspace jobId="job-1" />);

  await user.click(await screen.findByRole("button", { name: "截图 A 片段 2" }));
  fireEvent.change(screen.getByLabelText("截图 A 裁切上边界"), { target: { value: "45" } });
  await user.click(screen.getByRole("button", { name: "保存两侧裁切范围" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("crop save failed");
  expect(screen.getByRole("button", { name: "截图 A 片段 2" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByLabelText("截图 A 裁切上边界")).toHaveValue("45");
});

test("shows and resolves a pending relationship when the candidate queue is empty", async () => {
  const relationship = item.relationships[0];
  const primary = { ...item, correct_answer: "A" };
  const secondary = { ...second, correct_answer: "B" };
  const savedPrimary = { ...primary, parts: [{ ...primary.parts[0], source_y_start: 30 }] };
  const savedSecondary = { ...secondary, parts: [{ ...secondary.parts[0], source_y_end: 150 }] };
  const emptyBundle = { items: [], relationships: [], ordering_issues: [] };
  const fetchMock = vi
    .spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(response({ items: [], relationships: [relationship], ordering_issues: [] }))
    .mockResolvedValueOnce(response(primary))
    .mockResolvedValueOnce(response(secondary))
    .mockResolvedValueOnce(response({
      ...savedPrimary,
      primary: savedPrimary,
      secondary: savedSecondary,
    }))
    .mockResolvedValueOnce(response(primary))
    .mockResolvedValue(response(emptyBundle));
  vi.spyOn(window, "confirm").mockReturnValue(true);
  render(<ReviewWorkspace jobId="job-1" />);

  expect(await screen.findByRole("region", { name: "跨截图建议详情" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "截图 A 片段 1" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "截图 B 片段 1" })).not.toBeInTheDocument();
  expect(screen.getByLabelText("课程筛选")).toHaveValue("医学");
  expect(screen.getByLabelText("分类筛选")).toHaveValue("内科/呼吸");
  expect(screen.getByLabelText("批次筛选")).toHaveValue("异常批次");
  expect(screen.getByAltText("截图 A：异常.png")).toHaveAttribute("src", "/source");
  expect(screen.getByAltText("截图 B：异常.png")).toHaveAttribute("src", "/source");
  expect(screen.getAllByText("合并预览")).toHaveLength(2);
  expect(screen.getByText("识别答案：A")).toBeInTheDocument();
  expect(screen.getByText("识别答案：B")).toBeInTheDocument();
  expect(screen.getAllByText("建议裁切范围：20–120")).toHaveLength(2);

  fireEvent.change(screen.getByLabelText("截图 A 裁切上边界"), { target: { value: "30" } });
  fireEvent.change(screen.getByLabelText("截图 B 裁切下边界"), { target: { value: "150" } });
  fireEvent.click(screen.getByRole("button", { name: "保存两侧裁切范围" }));
  await waitFor(() => expect(fetchMock.mock.calls.filter(
    ([url, init]) => String(url) === "/api/review-relationships/relationship-1/crops" && init?.method === "PATCH",
  )).toHaveLength(1));
  const cropCall = fetchMock.mock.calls.find(
    ([url]) => String(url) === "/api/review-relationships/relationship-1/crops",
  );
  expect(JSON.parse(String((cropCall?.[1] as RequestInit).body))).toEqual({
    primary_parts: [{ id: "part-1", part_index: 0, source_y_start: 30, source_y_end: 120 }],
    secondary_parts: [{ id: "part-2", part_index: 0, source_y_start: 20, source_y_end: 150 }],
  });
  expect(fetchMock.mock.calls.some(
    ([url, init]) => (
      ["/api/review-items/candidate-1", "/api/review-items/candidate-2"].includes(String(url))
      && init?.method === "PATCH"
    ),
  )).toBe(false);

  fireEvent.click(screen.getByRole("button", { name: "确认跨截图合并" }));

  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url) === "/api/review-items/candidate-1/merge")).toBe(true));
  const mergeCall = fetchMock.mock.calls.find(([url]) => String(url) === "/api/review-items/candidate-1/merge");
  expect(JSON.parse(String((mergeCall?.[1] as RequestInit).body))).toEqual({
    secondary_candidate_id: "candidate-2",
    relationship_id: "relationship-1",
    confirmed: true,
  });
});


test("shows the true empty state only when all review queues are empty", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(response({
    items: [],
    relationships: [],
    ordering_issues: [],
  }));
  render(<ReviewWorkspace jobId="job-1" />);

  expect(await screen.findByText("当前筛选条件下没有待处理内容。")).toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "跨截图建议详情" })).not.toBeInTheDocument();
});


test("counts and opens an ordering-only issue then reloads the review queue", async () => {
  const orderingIssue = {
    id: "order-only-1",
    batch_id: "ordering-job",
    batch_name: "顺序异常批次",
    course_name: "独立课程",
    category_path: "第一章",
    code: "NEAR_TIE",
    message: "需要人工确认截图顺序",
    image_ids: ["one", "two"],
    status: "pending",
  };
  const ordering = {
    batch_id: "ordering-job",
    status: "ordering_review",
    requires_review: true,
    images: [
      { id: "one", original_name: "第一页.png", order_index: 0, review_flag: null, duplicate_of_id: null, content_url: "/one" },
      { id: "two", original_name: "第二页.png", order_index: 1, review_flag: null, duplicate_of_id: null, content_url: "/two" },
    ],
    overlaps: [],
    issues: [orderingIssue],
  };
  let reviewQueueFetchCount = 0;
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (url.startsWith("/api/review-items?")) {
      reviewQueueFetchCount += 1;
      return response(reviewQueueFetchCount === 1
        ? { items: [], relationships: [], ordering_issues: [orderingIssue] }
        : { items: [], relationships: [], ordering_issues: [] });
    }
    if (url === "/api/import-jobs/ordering-job/ordering" && init?.method === "PUT") {
      return response({ ...ordering, status: "ready", requires_review: false, issues: [] });
    }
    if (url === "/api/import-jobs/ordering-job/ordering") return response(ordering);
    throw new Error(`unexpected fetch: ${url}`);
  });
  const user = userEvent.setup();
  render(<ReviewWorkspace />);

  expect(await screen.findByText("1 项待核对")).toBeInTheDocument();
  expect(screen.getByRole("option", { name: "独立课程" })).toBeInTheDocument();
  expect(screen.getByRole("option", { name: "第一章" })).toBeInTheDocument();
  expect(screen.getByRole("option", { name: "顺序异常批次" })).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "检查该批次截图顺序" }));
  expect(await screen.findByRole("heading", { name: "截图顺序检查" })).toBeVisible();
  await user.click(screen.getByRole("button", { name: "确认并保存顺序" }));

  await waitFor(() => expect(reviewQueueFetchCount).toBeGreaterThan(1));
  expect(fetchMock.mock.calls.some(([url, init]) =>
    String(url) === "/api/import-jobs/ordering-job/ordering" && init?.method === "PUT"
  )).toBe(true);
});


test("shows processed ordering issues without an ordering action", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(response({
    items: [],
    relationships: [],
    ordering_issues: [{
      id: "processed-order",
      batch_id: "processed-job",
      batch_name: "已处理批次",
      course_name: "课程",
      category_path: "",
      code: "NEAR_TIE",
      message: "截图顺序已人工确认",
      image_ids: ["one", "two"],
      status: "resolved",
    }],
  }));
  render(<ReviewWorkspace />);

  expect(await screen.findByText("截图顺序已人工确认")).toBeInTheDocument();
  expect(screen.getByText("1 项待核对")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "检查该批次截图顺序" })).not.toBeInTheDocument();
});


test("labels duplicate conflicts and resolves the existing relationship as duplicate", async () => {
  const duplicateRelationship = {
    ...item.relationships[0],
    relation_type: "same_batch_duplicate",
    evidence: { answer_conflict: true, text_score: 0.96 },
  };
  const duplicateItem = { ...item, relationships: [duplicateRelationship] };
  const fetchMock = mockReviewFetch(() => response({
    items: [duplicateItem, second],
    relationships: [duplicateRelationship],
    ordering_issues: [],
  }));
  vi.spyOn(window, "confirm").mockReturnValue(true);
  render(<ReviewWorkspace jobId="job-1" />);

  expect(await screen.findByRole("region", { name: "疑似重复题" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "确认标记关系为重复题" }));
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url) === "/api/review-items/candidate-2/duplicate")).toBe(true));
  const duplicateCall = fetchMock.mock.calls.find(([url]) => String(url) === "/api/review-items/candidate-2/duplicate");
  expect(JSON.parse(String((duplicateCall?.[1] as RequestInit).body))).toEqual({
    duplicate_of_id: "candidate-1",
    confirmed: true,
  });
});
