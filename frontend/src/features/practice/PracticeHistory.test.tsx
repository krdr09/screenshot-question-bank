import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { PracticeHistory } from "./PracticeHistory";

afterEach(() => vi.restoreAllMocks());

test("shows the explicit empty practice history returned by the real endpoint", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify({ items: [], total: 0, page: 1, page_size: 20 }), { status: 200 }),
  );

  render(<PracticeHistory onResume={() => undefined} />);

  expect(await screen.findByText("还没有历史练习。完成或保存一次刷题后会显示在这里。")).toBeInTheDocument();
  expect(globalThis.fetch).toHaveBeenCalledWith("/api/practice-sessions?page=1&page_size=20");
});

test("renders persisted active and submitted summaries and resumes by session UUID", async () => {
  const resume = vi.fn();
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify({
      items: [
        {
          id: "session-active", feedback_mode: "deferred", order_mode: "random", status: "active",
          actual_count: 3, answered_count: 1, correct_count: null, incorrect_count: null,
          unanswered_count: null, started_at: "2026-07-18T08:00:00Z", submitted_at: null,
        },
        {
          id: "session-submitted", feedback_mode: "immediate", order_mode: "sequential", status: "submitted",
          actual_count: 3, answered_count: 3, correct_count: 2, incorrect_count: 1,
          unanswered_count: 0, started_at: "2026-07-17T08:00:00Z", submitted_at: "2026-07-17T08:05:00Z",
        },
      ],
      total: 2, page: 1, page_size: 20,
    }), { status: 200 }),
  );
  const user = userEvent.setup();

  render(<PracticeHistory onResume={resume} />);

  expect(await screen.findByText("进行中 · 已答 1/3")).toBeInTheDocument();
  expect(screen.getByText("已提交 · 正确 2 · 错误 1 · 未答 0")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "继续练习" }));
  await user.click(screen.getByRole("button", { name: "查看结果" }));
  expect(resume.mock.calls).toEqual([["session-active"], ["session-submitted"]]);
  expect(screen.queryByText(/正确答案|秘密解析/)).not.toBeInTheDocument();
});
