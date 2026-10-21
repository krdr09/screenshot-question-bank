import { afterEach, expect, test, vi } from "vitest";

import { getPracticeCandidates, type PracticeCandidate } from "./api";


const candidate = (index: number): PracticeCandidate => ({
  id: `question-${index}`,
  question_type: "single_choice",
  stem: `题目 ${index}`,
  course_id: "course-1",
  course_name: "医学",
  category_id: null,
  category_path: "未分类",
  batch_id: "batch-1",
  batch_name: "批次",
  batch_position: index,
  source_display_number: null,
  in_wrong_book: false,
  has_answered: false,
});

afterEach(() => vi.restoreAllMocks());

test("loads every candidate page and preserves the real total", async () => {
  const firstPage = Array.from({ length: 200 }, (_, index) => candidate(index + 1));
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = new URL(String(input), "http://local.test");
    const page = Number(url.searchParams.get("page"));
    return new Response(JSON.stringify({
      items: page === 1 ? firstPage : [candidate(201)],
      total: 201,
      page,
      page_size: 200,
    }), { status: 200 });
  });

  const result = await getPracticeCandidates({ membership: "all" });

  expect(result.total).toBe(201);
  expect(result.items).toHaveLength(201);
  expect(fetchMock).toHaveBeenCalledTimes(2);
  expect(String(fetchMock.mock.calls[1]?.[0])).toContain("page=2");
});
