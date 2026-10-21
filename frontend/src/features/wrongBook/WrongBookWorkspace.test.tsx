import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { WrongBookWorkspace } from "./WrongBookWorkspace";
import type { WrongQuestion } from "./api";


const active: WrongQuestion = {
  question_id: "question-1", question_type: "single_choice", stem: "肺炎治疗错题", options: [{ label: "A", text: "甲" }, { label: "B", text: "乙" }],
  correct_answer: "A", explanation: "错题解析", course_id: "course-1", course_name: "医学", category_id: "cat-1", category_path: "内科/呼吸",
  batch_id: "batch-1", batch_name: "合成批次", batch_position: 2, source_display_number: "7", display_mode: "image_preferred", error_count: 3, mastered: false,
  image_parts: [{ part_index: 0, url: "/api/questions/question-1/parts/part-1/content" }],
};
const mastered: WrongQuestion = { ...active, question_id: "question-2", stem: "已掌握错题", mastered: true, error_count: 1, image_parts: [] };
const courses = { items: [{ id: "course-1", name: "医学", categories: [{ id: "cat-1", path: "内科/呼吸" }] }] };
const batches = { items: [{ id: "batch-1", name: "合成批次", status: "ready", course_id: "course-1", course_name: "医学", category_id: "cat-1", category_path: "内科/呼吸", question_count: 2 }] };

function response(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response;
}

function installFetch() {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (url === "/api/courses") return response(courses);
    if (url.startsWith("/api/batches")) return response(batches);
    if (url.includes("/master") && init?.method === "POST") return response({ question_id: "question-1", mastered: true });
    if (url.includes("/restore") && init?.method === "POST") return response({ question_id: "question-2", mastered: false });
    if (url.startsWith("/api/wrong-questions")) {
      return response({ items: url.includes("include_mastered=true") ? [active, mastered] : [active], total: url.includes("include_mastered=true") ? 2 : 1 });
    }
    return response({});
  });
}

afterEach(() => vi.restoreAllMocks());


test("filters wrong questions and shows authorized review details", async () => {
  const fetchMock = installFetch();
  render(<WrongBookWorkspace onStartPractice={vi.fn()} />);

  expect(await screen.findByRole("heading", { name: "错题本" })).toBeInTheDocument();
  expect(screen.getByText("累计答错 3 次")).toBeInTheDocument();
  expect(screen.getByText("正确答案：A．甲")).toBeInTheDocument();
  expect(screen.getByText("错题解析")).toBeInTheDocument();
  expect(screen.getByAltText("错题裁切图 1")).toHaveAttribute("src", active.image_parts[0].url);
  expect(fetchMock.mock.calls.some(([url]) => String(url).startsWith("/api/wrong-questions") && !String(url).includes("include_mastered=true"))).toBe(true);

  fireEvent.change(screen.getByLabelText("错题课程"), { target: { value: "course-1" } });
  await screen.findByRole("option", { name: "内科/呼吸" });
  fireEvent.change(screen.getByLabelText("错题分类/章节"), { target: { value: "cat-1" } });
  fireEvent.change(screen.getByLabelText("错题导入批次"), { target: { value: "batch-1" } });
  fireEvent.click(screen.getByLabelText("包含已掌握"));
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => {
    const value = String(url);
    return value.includes("course_id=course-1") && value.includes("category_id=cat-1") && value.includes("batch_id=batch-1") && value.includes("include_mastered=true");
  })).toBe(true));
  expect(await screen.findByText("已掌握错题")).toBeInTheDocument();
});


test("keeps an effective-category batch selectable after choosing its overriding category", async () => {
  const categoryBCourses = {
    items: [{ id: "course-1", name: "医学", categories: [{ id: "cat-1", path: "内科/呼吸" }, { id: "cat-2", path: "内科/循环" }] }],
  };
  const categoryBQuestion: WrongQuestion = {
    ...active,
    category_id: "cat-2",
    category_path: "内科/循环",
  };
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = new URL(String(input), "http://local.test");
    if (url.pathname === "/api/courses") return response(categoryBCourses);
    if (url.pathname === "/api/batches") {
      return response(url.searchParams.get("category_id") === "cat-2" ? batches : { items: [] });
    }
    if (url.pathname === "/api/wrong-questions") return response({ items: [categoryBQuestion], total: 1 });
    return response({});
  });
  render(<WrongBookWorkspace onStartPractice={vi.fn()} />);

  await screen.findByRole("option", { name: "医学" });
  fireEvent.change(screen.getByLabelText("错题课程"), { target: { value: "course-1" } });
  await screen.findByRole("option", { name: "内科/循环" });
  fireEvent.change(screen.getByLabelText("错题分类/章节"), { target: { value: "cat-2" } });
  expect(await screen.findByRole("option", { name: "合成批次" })).toHaveValue("batch-1");
  fireEvent.change(screen.getByLabelText("错题导入批次"), { target: { value: "batch-1" } });
  expect(screen.getByLabelText("错题导入批次")).toHaveValue("batch-1");
  expect(fetchMock.mock.calls.some(([input]) => {
    const url = new URL(String(input), "http://local.test");
    return url.pathname === "/api/batches"
      && url.searchParams.get("course_id") === "course-1"
      && url.searchParams.get("category_id") === "cat-2";
  })).toBe(true);
});


test("marks and restores through real endpoints while preserving selectable re-practice IDs", async () => {
  const fetchMock = installFetch();
  const startPractice = vi.fn();
  const user = userEvent.setup();
  render(<WrongBookWorkspace onStartPractice={startPractice} />);
  await screen.findByRole("checkbox", { name: /肺炎治疗错题/ });

  const start = screen.getByRole("button", { name: "刷选中错题" });
  expect(start).toBeDisabled();
  await user.click(screen.getByRole("checkbox", { name: /肺炎治疗错题/ }));
  expect(start).toBeEnabled();
  await user.click(start);
  expect(startPractice).toHaveBeenCalledWith(["question-1"]);

  await user.click(screen.getByRole("button", { name: "标记已掌握" }));
  await waitFor(() => expect(fetchMock.mock.calls.some(([url, init]) => String(url) === "/api/wrong-questions/question-1/master" && init?.method === "POST")).toBe(true));

  await user.click(screen.getByLabelText("包含已掌握"));
  expect(await screen.findByText("已掌握错题")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "恢复到错题本" }));
  await waitFor(() => expect(fetchMock.mock.calls.some(([url, init]) => String(url) === "/api/wrong-questions/question-2/restore" && init?.method === "POST")).toBe(true));
});


test("clears selected wrong questions that disappear after filtering", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (url === "/api/courses") return response(courses);
    if (url.startsWith("/api/batches")) return response(batches);
    if (url.startsWith("/api/wrong-questions")) {
      return response({ items: url.includes("course_id=course-1") ? [] : [active], total: url.includes("course_id=course-1") ? 0 : 1 });
    }
    return response({});
  });
  const user = userEvent.setup();
  render(<WrongBookWorkspace onStartPractice={vi.fn()} />);
  await user.click(await screen.findByRole("checkbox", { name: /肺炎治疗错题/ }));
  expect(screen.getByRole("button", { name: "刷选中错题" })).toBeEnabled();

  fireEvent.change(screen.getByLabelText("错题课程"), { target: { value: "course-1" } });

  await waitFor(() => expect(screen.getByRole("button", { name: "刷选中错题" })).toBeDisabled());
});


test("ignores an older wrong-book response that finishes after a newer filter", async () => {
  const pending: Array<(value: Response) => void> = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (url === "/api/courses") return response(courses);
    if (url.startsWith("/api/batches")) return response(batches);
    if (url.startsWith("/api/wrong-questions")) {
      return new Promise<Response>((resolve) => pending.push(resolve));
    }
    return response({});
  });
  render(<WrongBookWorkspace onStartPractice={vi.fn()} />);
  await screen.findByRole("option", { name: "医学" });
  await waitFor(() => expect(pending).toHaveLength(1));

  fireEvent.change(screen.getByLabelText("错题课程"), { target: { value: "course-1" } });
  await waitFor(() => expect(pending).toHaveLength(2));
  pending[1](response({ items: [], total: 0 }));
  expect(await screen.findByText("当前筛选下没有错题。")).toBeInTheDocument();

  pending[0](response({ items: [active], total: 1 }));
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(screen.queryByText("肺炎治疗错题")).not.toBeInTheDocument();
});
