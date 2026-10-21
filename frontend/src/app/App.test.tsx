import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { LAST_PRACTICE_SESSION_KEY } from "../features/practice/PracticeWorkspace";

describe("应用壳", () => {
  afterEach(() => {
    vi.useRealTimers();
    localStorage.clear();
    vi.restoreAllMocks();
  });

  it("显示中文产品名称和本地离线说明", () => {
    render(<App />);

    expect(
      screen.getByRole("heading", { name: "截图题库 OCR 与刷题系统" }),
    ).toBeInTheDocument();
    expect(screen.getByText("所有题目与图片仅保存在本机")).toBeInTheDocument();
  });

  it("阶段 10 仍保留真实刷题和错题本入口", () => {
    render(<App />);

    expect(screen.queryByRole("link", { name: "导入截图" })).not.toBeInTheDocument();
    expect(screen.getByText("阶段 10 · 自动化测试与最终验收")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "开始刷题" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "打开错题本" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查看练习历史" })).toBeInTheDocument();
  });

  it("显示并打开真实的阶段 10 备份中心", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ items: [] }), { status: 200 }),
    );
    const user = userEvent.setup();
    render(<App />);

    expect(screen.getByText("阶段 10 · 自动化测试与最终验收")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "备份恢复" })).toHaveAttribute("href", "#backup-center");
    await user.click(screen.getByRole("button", { name: "打开备份与恢复" }));
    expect(await screen.findByRole("heading", { name: "备份与恢复", level: 3 })).toBeInTheDocument();
  });

  it("刷新后自动打开并恢复最后一次持久化刷题会话", async () => {
    localStorage.setItem(LAST_PRACTICE_SESSION_KEY, "session-resume");
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/api/courses" || url.startsWith("/api/batches")) return new Response(JSON.stringify({ items: [] }), { status: 200 });
      if (url.startsWith("/api/practice-candidates")) return new Response(JSON.stringify({ items: [], total: 0, page: 1, page_size: 50 }), { status: 200 });
      if (url === "/api/practice-sessions/session-resume") return new Response(JSON.stringify({
        id: "session-resume", feedback_mode: "immediate", order_mode: "sequential", status: "active", requested_count: null, actual_count: 1,
        limit_adjusted: false, notice: null, started_at: "2026-07-14T00:00:00Z", submitted_at: null, answered_count: 0,
        items: [{ id: "item-resume", question_id: "question-resume", order_index: 0, question_type: "true_false", stem: "恢复后的判断题", options: [],
          display_mode: "text", source_display_number: null, course_id: "course-1", course_name: "医学", category_id: null, category_path: "未分类",
          batch_id: "batch-1", batch_name: "批次", batch_position: 1, image_parts: [], saved: false, locked: false, was_unanswered: false }],
      }), { status: 200 });
      return new Response(JSON.stringify({}), { status: 200 });
    });

    render(<App />);

    expect(await screen.findByRole("heading", { name: "恢复后的判断题" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "开始刷题" })).not.toBeInTheDocument();
  });

  it("在刷题工作区已打开时仍能从历史切换到另一个服务端会话", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/api/courses" || url.startsWith("/api/batches")) return new Response(JSON.stringify({ items: [] }), { status: 200 });
      if (url.startsWith("/api/practice-candidates")) return new Response(JSON.stringify({ items: [], total: 0, page: 1, page_size: 200 }), { status: 200 });
      if (url.startsWith("/api/practice-sessions?page=")) return new Response(JSON.stringify({
        items: [{ id: "history-session", feedback_mode: "deferred", order_mode: "sequential", status: "submitted", actual_count: 1,
          answered_count: 1, correct_count: 1, incorrect_count: 0, unanswered_count: 0, started_at: "2026-07-18T08:00:00Z", submitted_at: "2026-07-18T08:01:00Z" }],
        total: 1, page: 1, page_size: 20,
      }), { status: 200 });
      if (url === "/api/practice-sessions/history-session") return new Response(JSON.stringify({
        id: "history-session", feedback_mode: "deferred", order_mode: "sequential", status: "submitted", requested_count: 1, actual_count: 1,
        limit_adjusted: false, notice: null, started_at: "2026-07-18T08:00:00Z", submitted_at: "2026-07-18T08:01:00Z", answered_count: 1,
        items: [{ id: "history-item", question_id: "history-question", order_index: 0, question_type: "true_false", stem: "历史判断题", options: [],
          display_mode: "text", source_display_number: "1", course_id: "course", course_name: "课程", category_id: null, category_path: "未分类",
          batch_id: "batch", batch_name: "批次", batch_position: 1, image_parts: [], selected_answer: true, saved: true, locked: true,
          was_unanswered: false, is_correct: true, correct_answer: true, explanation: "历史解析" }],
      }), { status: 200 });
      if (url === "/api/practice-sessions/history-session/result") return new Response(JSON.stringify({
        session_id: "history-session", total_count: 1, correct_count: 1, incorrect_count: 0, unanswered_count: 0, accuracy: 1,
        items: [{ id: "history-item", question_id: "history-question", order_index: 0, question_type: "true_false", stem: "历史判断题", options: [],
          display_mode: "text", source_display_number: "1", course_id: "course", course_name: "课程", category_id: null, category_path: "未分类",
          batch_id: "batch", batch_name: "批次", batch_position: 1, image_parts: [], selected_answer: true, saved: true, locked: true,
          was_unanswered: false, is_correct: true, correct_answer: true, explanation: "历史解析" }],
      }), { status: 200 });
      return new Response(JSON.stringify({}), { status: 200 });
    });
    const user = userEvent.setup();
    render(<App />);

    await user.click(screen.getByRole("button", { name: "开始刷题" }));
    expect(await screen.findByRole("heading", { name: "刷题设置" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "查看练习历史" }));
    await user.click(await screen.findByRole("button", { name: "查看结果" }));

    expect(await screen.findByRole("heading", { name: "本次结果" })).toBeInTheDocument();
    expect(screen.getByText(/1\. 历史判断题/)).toBeInTheDocument();
  });

  it("选择嵌套截图目录后显示可编辑分类映射", async () => {
    const user = userEvent.setup();
    render(<App />);
    const input = screen.getByLabelText("选择截图文件夹");
    const first = new File(["image-a"], "a.png", { type: "image/png" });
    const second = new File(["image-b"], "b.jpg", { type: "image/jpeg" });
    Object.defineProperty(first, "webkitRelativePath", {
      value: "待导入题库/运筹学/第一章/作业1/a.png",
    });
    Object.defineProperty(second, "webkitRelativePath", {
      value: "待导入题库/运筹学/第一章/作业1/b.jpg",
    });

    await user.upload(input, [first, second]);

    expect(screen.getByDisplayValue("运筹学")).toBeInTheDocument();
    expect(screen.getByDisplayValue("第一章")).toBeInTheDocument();
    expect(screen.getByDisplayValue("作业1")).toBeInTheDocument();
    expect(screen.getByText("2 张图片")).toBeInTheDocument();
    await user.clear(screen.getByLabelText("课程"));
    await user.type(screen.getByLabelText("课程"), "管理科学");
    expect(screen.getByDisplayValue("管理科学")).toBeInTheDocument();
  });

  it("普通图片文件夹要求填写课程和批次后才能确认", async () => {
    const user = userEvent.setup();
    render(<App />);
    const input = screen.getByLabelText("选择截图文件夹");
    const file = new File(["image"], "a.png", { type: "image/png" });
    Object.defineProperty(file, "webkitRelativePath", {
      value: "截图/a.png",
    });

    await user.upload(input, file);

    const confirm = screen.getByRole("button", { name: "确认分类并创建导入任务" });
    expect(confirm).toBeDisabled();
    await user.type(screen.getByLabelText("课程"), "运筹学");
    await user.type(screen.getByLabelText("批次"), "临时作业");
    expect(confirm).toBeEnabled();
  });

  it("确认分类后创建任务并上传所选图片", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ id: "job-1" }), {
          status: 201,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ uploaded_count: 1, duplicate_count: 0, images: [] }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            id: "job-1",
            status: "queued",
            uploaded_count: 1,
            processed_count: 0,
            found_question_count: 0,
            high_confidence_count: 0,
            review_count: 0,
            error_count: 0,
            current_file: null,
            ocr_device: "mock",
            cancel_requested: false,
            error_log: [],
          }),
          { status: 202, headers: { "Content-Type": "application/json" } },
        ),
      );
    const user = userEvent.setup();
    render(<App />);
    const file = new File(["image"], "a.png", { type: "image/png" });
    Object.defineProperty(file, "webkitRelativePath", {
      value: "待导入题库/运筹学/作业1/a.png",
    });

    await user.upload(screen.getByLabelText("选择截图文件夹"), file);
    await user.click(
      screen.getByRole("button", { name: "确认分类并创建导入任务" }),
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/import-jobs");
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/import-jobs/job-1/files");
    expect(fetchMock.mock.calls[2]?.[0]).toBe("/api/import-jobs/job-1/process");
    expect(fetchMock.mock.calls[1]?.[1]?.body).toBeInstanceOf(FormData);
    expect(
      screen.getByText("已创建 1 个导入批次，共上传 1 张图片。"),
    ).toBeInTheDocument();
    expect(screen.getByText("等待处理")).toBeInTheDocument();
  });

  it("后续批次失败时保留已经启动的导入进度", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ id: "job-1" }), { status: 201 }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ uploaded_count: 1, duplicate_count: 0, images: [] }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            id: "job-1",
            status: "queued",
            uploaded_count: 1,
            processed_count: 0,
            found_question_count: 0,
            high_confidence_count: 0,
            review_count: 0,
            error_count: 0,
            current_file: null,
            ocr_device: "mock",
            cancel_requested: false,
            error_log: [],
          }),
          { status: 202 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ id: "job-2" }), { status: 201 }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "第二批图片损坏" }), { status: 400 }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            id: "job-1",
            status: "processing",
            uploaded_count: 1,
            processed_count: 0,
            found_question_count: 0,
            high_confidence_count: 0,
            review_count: 0,
            error_count: 0,
            current_file: "a.png",
            ocr_device: "mock",
            cancel_requested: false,
            error_log: [],
          }),
          { status: 200 },
        ),
      );

    const first = new File(["first"], "a.png", { type: "image/png" });
    const second = new File(["second"], "b.png", { type: "image/png" });
    Object.defineProperty(first, "webkitRelativePath", {
      value: "根目录/课程/第一批/a.png",
    });
    Object.defineProperty(second, "webkitRelativePath", {
      value: "根目录/课程/第二批/b.png",
    });

    const user = userEvent.setup();
    render(<App />);
    await user.upload(screen.getByLabelText("选择截图文件夹"), [first, second]);
    vi.useFakeTimers();
    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: "确认分类并创建导入任务" }),
      );
      for (let turn = 0; turn < 20; turn += 1) {
        await Promise.resolve();
      }
    });

    expect(screen.getByText("第二批图片损坏")).toBeInTheDocument();
    expect(screen.getByText("等待处理")).toBeInTheDocument();
    expect(screen.getByLabelText("导入处理进度")).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });

    expect(fetchMock).toHaveBeenLastCalledWith("/api/import-jobs/job-1");
    expect(screen.getByText("正在 OCR")).toBeInTheDocument();
    expect(screen.getByText("a.png")).toBeInTheDocument();
    expect(screen.getByText("第二批图片损坏")).toBeInTheDocument();
  });

  it("可以从首页打开全局待检查中心", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify({ items: [], relationships: [], ordering_issues: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const user = userEvent.setup();
    render(<App />);

    await user.click(screen.getByRole("button", { name: "打开待检查页面" }));

    expect(await screen.findByRole("heading", { name: "异常题人工检查" })).toBeInTheDocument();
    expect(globalThis.fetch).toHaveBeenCalledWith("/api/review-items?handling_status=pending");
  });

  it("可以从首页打开正式题库管理", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.startsWith("/api/courses")) return new Response(JSON.stringify({ items: [] }), { status: 200 });
      if (url.startsWith("/api/batches")) return new Response(JSON.stringify({ items: [] }), { status: 200 });
      return new Response(JSON.stringify({ items: [], total: 0, page: 1, page_size: 50 }), { status: 200 });
    });
    const user = userEvent.setup();
    render(<App />);

    await user.click(screen.getByRole("button", { name: "打开题库管理" }));

    expect(await screen.findByRole("heading", { name: "题库管理", level: 3 })).toBeInTheDocument();
  });

  it("把错题本选择的 UUID 交给真实刷题设置", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/api/courses") return new Response(JSON.stringify({ items: [] }), { status: 200 });
      if (url.startsWith("/api/batches")) return new Response(JSON.stringify({ items: [] }), { status: 200 });
      if (url.startsWith("/api/wrong-questions")) return new Response(JSON.stringify({ items: [{
        question_id: "question-1", question_type: "single_choice", stem: "首页错题", options: [{ label: "A", text: "甲" }], correct_answer: "A", explanation: "解析",
        course_id: "course-1", course_name: "医学", category_id: null, category_path: "未分类", batch_id: "batch-1", batch_name: "批次", batch_position: 1,
        source_display_number: null, display_mode: "text", error_count: 1, mastered: false, image_parts: [],
      }], total: 1 }), { status: 200 });
      if (url.startsWith("/api/practice-candidates")) return new Response(JSON.stringify({ items: [{
        id: "question-1", question_type: "single_choice", stem: "首页错题", course_id: "course-1", course_name: "医学", category_id: null, category_path: "未分类",
        batch_id: "batch-1", batch_name: "批次", batch_position: 1, source_display_number: null, in_wrong_book: true, has_answered: true,
      }], total: 1, page: 1, page_size: 50 }), { status: 200 });
      return new Response(JSON.stringify({}), { status: 200 });
    });
    const user = userEvent.setup();
    render(<App />);

    await user.click(screen.getByRole("button", { name: "打开错题本" }));
    await user.click(await screen.findByRole("checkbox", { name: /首页错题/ }));
    await user.click(screen.getByRole("button", { name: "刷选中错题" }));

    expect(await screen.findByRole("heading", { name: "刷题设置" })).toBeInTheDocument();
    expect(await screen.findByRole("checkbox", { name: /首页错题/ })).toBeChecked();
  });
});
