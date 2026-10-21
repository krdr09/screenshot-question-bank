import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { OrderingReview } from "./OrderingReview";


const ordering = {
  batch_id: "job-1",
  status: "ordering_review",
  requires_review: true,
  images: [
    { id: "one", original_name: "第一页.png", order_index: 0, review_flag: null, duplicate_of_id: null, content_url: "/one" },
    { id: "two", original_name: "第二页.png", order_index: 1, review_flag: null, duplicate_of_id: null, content_url: "/two" },
  ],
  overlaps: [
    { source_image_id: "one", target_image_id: "two", text_score: 0.8, visual_score: 0.9, combined_score: 0.84, overlap_ratio: 0.3, matched_text: ["重叠内容"] },
  ],
  issues: [{ id: "issue-1", code: "NEAR_TIE", message: "候选接近", image_ids: ["one", "two"], status: "pending" }],
};


function response(body: unknown, ok = true): Response {
  return { ok, status: ok ? 200 : 500, json: async () => body } as Response;
}


test("loads uncertain order and supports reordering comparison flags and confirmation", async () => {
  const fetchMock = vi
    .spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(response(ordering))
    .mockResolvedValueOnce(response({ ...ordering, status: "ready", requires_review: false }));
  const confirmed = vi.fn();
  render(<OrderingReview jobId="job-1" onConfirmed={confirmed} />);

  expect(await screen.findByRole("heading", { name: "截图顺序检查" })).toBeInTheDocument();
  expect(screen.getByText("84%" )).toBeInTheDocument();
  expect(screen.getByText("候选接近")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "下移 第一页.png" }));
  expect(screen.getAllByRole("listitem")[0]).toHaveTextContent("第二页.png");
  expect(screen.getByText("无可靠匹配")).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("标记 第二页.png"), { target: { value: "unrelated" } });
  fireEvent.click(screen.getByRole("button", { name: "对比 第二页.png 和 第一页.png" }));
  expect(screen.getByAltText("对比图 第二页.png")).toBeInTheDocument();
  expect(screen.getByAltText("对比图 第一页.png")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "确认并保存顺序" }));

  await waitFor(() => expect(confirmed).toHaveBeenCalled());
  const request = fetchMock.mock.calls[1][1] as RequestInit;
  expect(JSON.parse(request.body as string)).toEqual({
    items: [
      { image_id: "two", review_flag: "unrelated" },
      { image_id: "one", review_flag: null },
    ],
  });
});


test("supports drag ordering and exposes server errors", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(response(ordering));
  const { rerender } = render(<OrderingReview jobId="job-1" />);
  await screen.findByRole("button", { name: "下移 第一页.png" });
  fireEvent.dragStart(screen.getByText("第一页.png", { selector: "strong" }), { dataTransfer: { setData: vi.fn(), getData: () => "one" } });
  fireEvent.drop(screen.getByText("第二页.png", { selector: "strong" }), { dataTransfer: { getData: () => "one" } });
  expect(screen.getAllByRole("listitem")[1]).toHaveTextContent("第一页.png");

  fetchMock.mockReset().mockResolvedValueOnce(response({ detail: "读取失败" }, false));
  rerender(<OrderingReview jobId="job-2" />);
  expect(await screen.findByRole("alert")).toHaveTextContent("读取失败");
});
