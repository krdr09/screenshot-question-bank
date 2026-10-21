import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { LAST_PRACTICE_SESSION_KEY, PracticeWorkspace } from "./PracticeWorkspace";
import type { PracticeItem, PracticeSession } from "./api";


const courses = {
  items: [{ id: "course-1", name: "医学", categories: [{ id: "cat-1", path: "内科/呼吸" }] }],
};
const batches = {
  items: [{ id: "batch-1", name: "合成批次", status: "ready", course_id: "course-1", course_name: "医学", category_id: "cat-1", category_path: "内科/呼吸", question_count: 2 }],
};
const candidates = {
  items: [
    { id: "question-1", question_type: "single_choice", stem: "A-E 单选题", course_id: "course-1", course_name: "医学", category_id: "cat-1", category_path: "内科/呼吸", batch_id: "batch-1", batch_name: "合成批次", batch_position: 2, source_display_number: "7", in_wrong_book: false, has_answered: false },
    { id: "question-2", question_type: "true_false", stem: "反向判断题", course_id: "course-1", course_name: "医学", category_id: "cat-1", category_path: "内科/呼吸", batch_id: "batch-1", batch_name: "合成批次", batch_position: 3, source_display_number: "8", in_wrong_book: false, has_answered: true },
  ],
  total: 2,
  page: 1,
  page_size: 50,
};
const choiceItem: PracticeItem = {
  id: "item-1", question_id: "question-1", order_index: 0, question_type: "single_choice", stem: "A-E 单选题",
  options: ["A", "B", "C", "D", "E"].map((label) => ({ label, text: `选项 ${label}` })), display_mode: "image_preferred", source_display_number: "7",
  course_id: "course-1", course_name: "医学", category_id: "cat-1", category_path: "内科/呼吸", batch_id: "batch-1", batch_name: "合成批次", batch_position: 2,
  image_parts: [{ part_index: 0, source_y_start: 10, source_y_end: 120, url: "/api/practice/crop-1" }], saved: false, locked: false, was_unanswered: false,
};
const booleanItem: PracticeItem = {
  ...choiceItem, id: "item-2", question_id: "question-2", order_index: 1, question_type: "true_false", stem: "反向判断题",
  options: [{ label: "A", text: "错误" }, { label: "B", text: "正确" }], display_mode: "text", source_display_number: "8", batch_position: 3, image_parts: [],
};

function session(feedbackMode: "immediate" | "deferred"): PracticeSession {
  return {
    id: `session-${feedbackMode}`, feedback_mode: feedbackMode, order_mode: "sequential", status: "active", requested_count: 2, actual_count: 2,
    limit_adjusted: true, notice: "请求 5 题，当前可用 2 题，已使用全部 2 题。", started_at: "2026-07-14T00:00:00Z", submitted_at: null, answered_count: 0,
    items: [choiceItem, booleanItem],
  };
}

function response(body: unknown, ok = true): Response {
  return { ok, status: ok ? 200 : 422, json: async () => body } as Response;
}

function installFetch(
  feedbackMode: "immediate" | "deferred" = "immediate",
  firstItem: PracticeItem = choiceItem,
) {
  let active = { ...session(feedbackMode), items: [firstItem, booleanItem] };
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (url === "/api/courses") return response(courses);
    if (url.startsWith("/api/batches")) return response(batches);
    if (url.startsWith("/api/practice-candidates")) return response(candidates);
    if (url === "/api/practice-sessions" && init?.method === "POST") return response(active);
    if (url === `/api/practice-sessions/${active.id}` && !init?.method) return response(active);
    if (url.endsWith("/answers") && init?.method === "POST") {
      const body = JSON.parse(String(init.body)) as { practice_item_id: string; selected_answer: string | boolean };
      const source = active.items.find((item) => item.id === body.practice_item_id)!;
      const answered: PracticeItem = {
        ...source, selected_answer: body.selected_answer, saved: true,
        locked: feedbackMode === "immediate",
        ...(feedbackMode === "immediate" ? { is_correct: body.selected_answer === "E" || body.selected_answer === true, correct_answer: source.question_type === "single_choice" ? "E" : true, explanation: "服务端解析" } : {}),
      };
      active = { ...active, answered_count: active.answered_count + (source.saved ? 0 : 1), items: active.items.map((item) => item.id === answered.id ? answered : item) };
      return response(answered);
    }
    if (url.endsWith("/submit") && init?.method === "POST") return response({
      session_id: active.id, total_count: 2, correct_count: 1, incorrect_count: 0, unanswered_count: 1, accuracy: 50,
      items: [
        { ...active.items[0], locked: true, is_correct: true, correct_answer: "E", explanation: "服务端解析" },
        { ...active.items[1], locked: true, was_unanswered: true, correct_answer: true, explanation: "判断解析" },
      ],
    });
    return response(active);
  });
}

beforeEach(() => localStorage.clear());
afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});


test("renders complete settings and sends manual IDs without losing server limit notice", async () => {
  const fetchMock = installFetch();
  const user = userEvent.setup();
  render(<PracticeWorkspace initialQuestionIds={["question-1"]} />);

  expect(screen.getByLabelText("课程")).toBeInTheDocument();
  expect(screen.getByLabelText("分类/章节")).toBeInTheDocument();
  expect(screen.getByLabelText("导入批次")).toBeInTheDocument();
  expect(screen.getByLabelText("只刷未做题")).toBeInTheDocument();
  expect(screen.getByLabelText("顺序出题")).toBeInTheDocument();
  expect(screen.getByLabelText("逐题即时反馈")).toBeInTheDocument();
  expect(await screen.findByRole("checkbox", { name: /A-E 单选题/ })).toBeChecked();

  await user.click(screen.getByRole("button", { name: "开始本次刷题" }));
  await screen.findByRole("heading", { name: "A-E 单选题" });
  expect(screen.getByText(/请求 5 题/)).toBeInTheDocument();

  const create = fetchMock.mock.calls.find(([url, init]) => String(url) === "/api/practice-sessions" && init?.method === "POST")?.[1];
  expect(JSON.parse(String(create?.body))).toMatchObject({
    selection: { manual_question_ids: ["question-1"] },
    order_mode: "sequential",
    feedback_mode: "immediate",
  });
  expect(JSON.parse(String(create?.body))).not.toHaveProperty("count");
  expect(localStorage.getItem(LAST_PRACTICE_SESSION_KEY)).toBe("session-immediate");
});


test("keeps an effective-category batch selectable after choosing its overriding category", async () => {
  const categoryBCourses = {
    items: [{ id: "course-1", name: "医学", categories: [{ id: "cat-1", path: "内科/呼吸" }, { id: "cat-2", path: "内科/循环" }] }],
  };
  const categoryBCandidates = {
    items: [{ ...candidates.items[0], category_id: "cat-2", category_path: "内科/循环" }],
    total: 1,
    page: 1,
    page_size: 50,
  };
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = new URL(String(input), "http://local.test");
    if (url.pathname === "/api/courses") return response(categoryBCourses);
    if (url.pathname === "/api/batches") {
      return response(url.searchParams.get("category_id") === "cat-2" ? batches : { items: [] });
    }
    if (url.pathname === "/api/practice-candidates") return response(categoryBCandidates);
    return response({});
  });
  render(<PracticeWorkspace />);

  await screen.findByRole("option", { name: "医学" });
  fireEvent.change(screen.getByLabelText("课程"), { target: { value: "course-1" } });
  await screen.findByRole("option", { name: "内科/循环" });
  fireEvent.change(screen.getByLabelText("分类/章节"), { target: { value: "cat-2" } });
  expect(await screen.findByRole("option", { name: "合成批次" })).toHaveValue("batch-1");
  fireEvent.change(screen.getByLabelText("导入批次"), { target: { value: "batch-1" } });
  expect(screen.getByLabelText("导入批次")).toHaveValue("batch-1");
  expect(fetchMock.mock.calls.some(([input]) => {
    const url = new URL(String(input), "http://local.test");
    return url.pathname === "/api/batches"
      && url.searchParams.get("course_id") === "course-1"
      && url.searchParams.get("category_id") === "cat-2";
  })).toBe(true);
});


test("offers all or custom quantity and only sends a count for a custom quantity", async () => {
  const fetchMock = installFetch();
  const user = userEvent.setup();
  render(<PracticeWorkspace />);
  await screen.findByRole("checkbox", { name: /A-E 单选题/ });

  expect(screen.getByLabelText("全部题目")).toBeChecked();
  expect(screen.getByLabelText("自定义题目数量")).toBeDisabled();
  await user.click(screen.getByLabelText("自定义数量"));
  await user.clear(screen.getByLabelText("自定义题目数量"));
  await user.type(screen.getByLabelText("自定义题目数量"), "1");
  await user.click(screen.getByRole("button", { name: "开始本次刷题" }));

  const create = fetchMock.mock.calls.find(([url, init]) => String(url) === "/api/practice-sessions" && init?.method === "POST")?.[1];
  expect(JSON.parse(String(create?.body))).toMatchObject({ count: 1 });
});


test("sends batch positions and uses only server feedback for choice and boolean answers", async () => {
  const fetchMock = installFetch();
  const user = userEvent.setup();
  render(<PracticeWorkspace />);
  await screen.findByRole("checkbox", { name: /A-E 单选题/ });

  fireEvent.change(screen.getByLabelText("导入批次"), { target: { value: "batch-1" } });
  expect(await screen.findByText("可用内部位置 2–3")).toBeInTheDocument();
  await user.clear(screen.getByLabelText("起始位置"));
  await user.type(screen.getByLabelText("起始位置"), "2");
  expect(screen.getByRole("button", { name: "开始本次刷题" })).toBeDisabled();
  expect(screen.getByText("请同时填写起始位置和结束位置。")).toBeInTheDocument();
  await user.clear(screen.getByLabelText("结束位置"));
  await user.type(screen.getByLabelText("结束位置"), "3");
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => {
    const value = String(url);
    return value.includes("position_start=2") && value.includes("position_end=3");
  })).toBe(true));
  await user.click(screen.getByRole("button", { name: "开始本次刷题" }));

  const create = fetchMock.mock.calls.find(([url, init]) => String(url) === "/api/practice-sessions" && init?.method === "POST")?.[1];
  expect(JSON.parse(String(create?.body)).selection).toMatchObject({ batch_id: "batch-1", position_start: 2, position_end: 3 });

  await user.click(screen.getByRole("radio", { name: /选项 E/ }));
  expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/answers"))).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: "提交本题" }));
  expect(await screen.findByText("回答正确")).toBeInTheDocument();
  expect(screen.getByRole("radio", { name: /选项 A/ })).toBeDisabled();
  expect(screen.getByText("服务端解析")).toBeInTheDocument();
  expect(screen.getByAltText("题目图片 1")).toHaveAttribute("src", "/api/practice/crop-1");
  await user.click(screen.getByRole("button", { name: "放大题图" }));
  expect(screen.getByTestId("practice-image-scale")).toHaveTextContent("110%");
  await user.click(screen.getByRole("button", { name: "下一题" }));
  await user.click(screen.getByRole("button", { name: /正确/ }));
  expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/answers"))).toHaveLength(1);
  await user.click(screen.getByRole("button", { name: "提交本题" }));

  const answerBodies = fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/answers")).map(([, init]) => JSON.parse(String(init?.body)));
  expect(answerBodies).toEqual([
    { practice_item_id: "item-1", selected_answer: "E" },
    { practice_item_id: "item-2", selected_answer: true },
  ]);

});


test("restores the persisted active session after remounting", async () => {
  const fetchMock = installFetch();
  localStorage.setItem(LAST_PRACTICE_SESSION_KEY, "session-immediate");

  render(<PracticeWorkspace />);

  expect(await screen.findByRole("heading", { name: "A-E 单选题" })).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith("/api/practice-sessions/session-immediate");
  expect(screen.getByText("QUESTION 1 / 2")).toBeInTheDocument();
});


test("keeps the persisted session id when a transient restore request fails", async () => {
  localStorage.setItem(LAST_PRACTICE_SESSION_KEY, "session-transient");
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (url === "/api/practice-sessions/session-transient") {
      return { ok: false, status: 503, json: async () => ({ detail: "服务暂时不可用" }) } as Response;
    }
    if (url === "/api/courses" || url.startsWith("/api/batches")) return response({ items: [] });
    if (url.startsWith("/api/practice-candidates")) return response({ items: [], total: 0, page: 1, page_size: 200 });
    return response({});
  });

  render(<PracticeWorkspace />);

  expect(await screen.findByRole("alert")).toHaveTextContent("无法恢复上次刷题：服务暂时不可用");
  expect(localStorage.getItem(LAST_PRACTICE_SESSION_KEY)).toBe("session-transient");
});


test("ignores an older candidate response that finishes after a newer filter", async () => {
  const pending: Array<(value: Response) => void> = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (url === "/api/courses") return response(courses);
    if (url.startsWith("/api/batches")) return response(batches);
    if (url.startsWith("/api/practice-candidates")) {
      return new Promise<Response>((resolve) => pending.push(resolve));
    }
    return response({});
  });
  render(<PracticeWorkspace />);
  await screen.findByRole("option", { name: "医学" });
  await waitFor(() => expect(pending).toHaveLength(1));

  fireEvent.change(screen.getByLabelText("课程"), { target: { value: "course-1" } });
  await waitFor(() => expect(pending).toHaveLength(2));
  pending[1](response({ items: [], total: 0, page: 1, page_size: 200 }));
  expect(await screen.findByText("当前匹配 0 题")).toBeInTheDocument();

  pending[0](response(candidates));
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(screen.getByText("当前匹配 0 题")).toBeInTheDocument();
  expect(screen.queryByText(/A-E 单选题 · 批次位置/)).not.toBeInTheDocument();
});


test("keeps deferred answers editable and shows the server result breakdown after confirmation", async () => {
  installFetch("deferred");
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const openWrongBook = vi.fn();
  const user = userEvent.setup();
  render(<PracticeWorkspace onOpenWrongBook={openWrongBook} />);
  await screen.findByRole("checkbox", { name: /A-E 单选题/ });
  await user.click(screen.getByLabelText("整套完成后反馈"));
  await user.click(screen.getByRole("button", { name: "开始本次刷题" }));

  await user.click(screen.getByRole("radio", { name: /选项 B/ }));
  expect(screen.queryByText("回答正确")).not.toBeInTheDocument();
  expect(screen.queryByText("服务端解析")).not.toBeInTheDocument();
  expect(screen.getByRole("radio", { name: /选项 B/ })).not.toBeDisabled();
  await user.click(screen.getByRole("radio", { name: /选项 E/ }));
  await user.click(screen.getByRole("button", { name: "提交整套答案" }));

  expect(await screen.findByRole("heading", { name: "本次结果" })).toBeInTheDocument();
  const summary = screen.getByTestId("practice-result-summary");
  expect(summary).toHaveTextContent("总题数 2");
  expect(summary).toHaveTextContent("正确 1");
  expect(summary).toHaveTextContent("错误 0");
  expect(summary).toHaveTextContent("未作答 1");
  expect(summary).toHaveTextContent("正确率 50.00%");
  expect(screen.getByText("你的答案：E．选项 E")).toBeInTheDocument();
  expect(screen.getByText("正确答案：E．选项 E")).toBeInTheDocument();
  expect(screen.getByText("未作答", { selector: ".practice-result-state" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "进入错题本" }));
  expect(openWrongBook).toHaveBeenCalledOnce();
  await waitFor(() => expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("未作答")));
});


test("shows the option text beside a choice label in immediate feedback", async () => {
  installFetch("immediate");
  const user = userEvent.setup();
  render(<PracticeWorkspace />);

  await screen.findByRole("checkbox", { name: /A-E 单选题/ });
  await user.click(screen.getByRole("button", { name: "开始本次刷题" }));
  await user.click(screen.getByRole("radio", { name: /选项 E/ }));
  await user.click(screen.getByRole("button", { name: "提交本题" }));

  expect(await screen.findByText("正确答案：E．选项 E")).toBeInTheDocument();
});


test("treats manual image preference as image preferred in practice and results", async () => {
  installFetch("deferred", { ...choiceItem, display_mode: "image_first" });
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const user = userEvent.setup();
  render(<PracticeWorkspace />);

  await screen.findByRole("checkbox", { name: /A-E 单选题/ });
  await user.click(screen.getByLabelText("整套完成后反馈"));
  await user.click(screen.getByRole("button", { name: "开始本次刷题" }));

  expect(screen.getByText("此题建议结合裁切图片核对，OCR 文字仍保留供参考。")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "收起题图" })).toBeInTheDocument();
  expect(screen.getByAltText("题目图片 1")).toHaveAttribute("src", "/api/practice/crop-1");

  await user.click(screen.getByRole("radio", { name: /选项 E/ }));
  await user.click(screen.getByRole("button", { name: "提交整套答案" }));

  expect(await screen.findByRole("heading", { name: "本次结果" })).toBeInTheDocument();
  expect(screen.getByText("此题建议结合裁切图片核对，OCR 文字仍保留供参考。")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "收起题图" })).toBeInTheDocument();
  expect(screen.getByAltText("题目图片 1")).toHaveAttribute("src", "/api/practice/crop-1");
});


test("keeps manual text preference collapsed until the user opens the image", async () => {
  installFetch("immediate", { ...choiceItem, display_mode: "text_first" });
  const user = userEvent.setup();
  render(<PracticeWorkspace />);

  await screen.findByRole("checkbox", { name: /A-E 单选题/ });
  await user.click(screen.getByRole("button", { name: "开始本次刷题" }));

  expect(screen.queryByText("此题建议结合裁切图片核对，OCR 文字仍保留供参考。")).not.toBeInTheDocument();
  expect(screen.queryByAltText("题目图片 1")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "查看题图" }));
  expect(screen.getByAltText("题目图片 1")).toHaveAttribute("src", "/api/practice/crop-1");
});
