import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ImportProgress } from "./ImportProgress";
import type { ImportJobProgress } from "./api";


const queued: ImportJobProgress = {
  id: "job-1",
  status: "queued",
  uploaded_count: 3,
  processed_count: 1,
  found_question_count: 2,
  high_confidence_count: 1,
  review_count: 1,
  error_count: 0,
  current_file: "normal-02.png",
  ocr_device: "mock",
  cancel_requested: false,
  error_log: [],
};


describe("导入处理进度", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("显示持久化任务的完整进度字段", () => {
    render(<ImportProgress initial={queued} />);

    expect(screen.getByText("等待处理")).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("value", "1");
    expect(screen.getByRole("progressbar")).toHaveAttribute("max", "3");
    expect(screen.getByText("normal-02.png")).toBeInTheDocument();
    expect(screen.getByText("Mock OCR")).toBeInTheDocument();
    expect(screen.getByText("已找到 2")).toBeInTheDocument();
    expect(screen.getByText("高置信度 1")).toBeInTheDocument();
    expect(screen.getByText("待检查 1")).toBeInTheDocument();
    expect(screen.getByText("错误 0")).toBeInTheDocument();
  });

  it("取消只调用当前任务并更新状态", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(
        JSON.stringify({ ...queued, status: "cancelled", cancel_requested: true }),
        { status: 202, headers: { "Content-Type": "application/json" } },
      ),
    );
    const user = userEvent.setup();
    render(<ImportProgress initial={queued} />);

    await user.click(screen.getByRole("button", { name: "取消此任务" }));

    expect(fetchMock).toHaveBeenCalledWith("/api/import-jobs/job-1/cancel", {
      method: "POST",
    });
    expect(await screen.findByText("已取消")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "取消此任务" })).not.toBeInTheDocument();
  });

  it("题目待检查状态接入真实异常复核工作台", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify({ items: [], relationships: [], ordering_issues: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    render(<ImportProgress initial={{ ...queued, status: "question_review" }} />);

    expect(await screen.findByRole("heading", { name: "异常题人工检查" })).toBeInTheDocument();
  });
});
