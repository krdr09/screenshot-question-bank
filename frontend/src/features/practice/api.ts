export type PracticeOption = { label: string; text: string; order_index?: number };
export type PracticeImagePart = {
  part_index: number;
  source_y_start: number;
  source_y_end: number;
  url: string;
};
export type PracticeCandidate = {
  id: string;
  question_type: "single_choice" | "true_false";
  stem: string;
  course_id: string;
  course_name: string;
  category_id: string | null;
  category_path: string;
  batch_id: string;
  batch_name: string;
  batch_position: number;
  source_display_number: string | null;
  in_wrong_book: boolean;
  has_answered: boolean;
};
export type PracticeCandidateCollection = {
  items: PracticeCandidate[];
  total: number;
};
export type PracticeItem = {
  id: string;
  question_id: string;
  order_index: number;
  question_type: "single_choice" | "true_false";
  stem: string;
  options: PracticeOption[];
  display_mode: string;
  source_display_number: string | null;
  course_id: string;
  course_name: string;
  category_id: string | null;
  category_path: string;
  batch_id: string;
  batch_name: string;
  batch_position: number;
  image_parts: PracticeImagePart[];
  selected_answer?: boolean | string | null;
  saved: boolean;
  locked: boolean;
  was_unanswered: boolean;
  is_correct?: boolean | null;
  correct_answer?: boolean | string | null;
  explanation?: string | null;
};
export type PracticeSession = {
  id: string;
  feedback_mode: "immediate" | "deferred";
  order_mode: "sequential" | "random";
  status: "active" | "submitted";
  requested_count: number | null;
  actual_count: number;
  limit_adjusted: boolean;
  notice?: string | null;
  started_at: string;
  submitted_at: string | null;
  answered_count: number;
  items: PracticeItem[];
};
export type PracticeResult = {
  session_id: string;
  total_count: number;
  correct_count: number;
  incorrect_count: number;
  unanswered_count: number;
  accuracy: number;
  items: PracticeItem[];
};
export type PracticeHistoryItem = {
  id: string;
  feedback_mode: "immediate" | "deferred";
  order_mode: "sequential" | "random";
  status: "active" | "submitted";
  actual_count: number;
  answered_count: number;
  correct_count: number | null;
  incorrect_count: number | null;
  unanswered_count: number | null;
  started_at: string;
  submitted_at: string | null;
};
export type PracticeHistoryPage = {
  items: PracticeHistoryItem[];
  total: number;
  page: number;
  page_size: number;
};
export type PracticeSelection = {
  course_id?: string;
  category_id?: string;
  batch_id?: string;
  question_type?: "single_choice" | "true_false";
  membership?: "all" | "wrong" | "unanswered";
  manual_question_ids?: string[];
  position_start?: number;
  position_end?: number;
};
export type CreatePracticePayload = {
  selection: PracticeSelection;
  count?: number;
  order_mode: "sequential" | "random";
  feedback_mode: "immediate" | "deferred";
};

export class PracticeApiError extends Error {
  constructor(message: string, public readonly status: number) {
    super(message);
    this.name = "PracticeApiError";
  }
}

async function parse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new PracticeApiError(body?.detail ?? `请求失败（HTTP ${response.status}）`, response.status);
  }
  return response.json() as Promise<T>;
}

function candidateQuery(filters: PracticeSelection, page: number): URLSearchParams {
  const query = new URLSearchParams({ page: String(page), page_size: "200" });
  for (const [key, value] of Object.entries(filters)) {
    if (key === "manual_question_ids" || value === undefined || value === "" || Array.isArray(value)) continue;
    query.set(key, String(value));
  }
  return query;
}

export async function getPracticeCandidates(filters: PracticeSelection): Promise<PracticeCandidateCollection> {
  const first = await parse<{ items: PracticeCandidate[]; total: number }>(
    await fetch(`/api/practice-candidates?${candidateQuery(filters, 1).toString()}`),
  );
  const pageCount = Math.ceil(first.total / 200);
  const remaining = await Promise.all(
    Array.from({ length: Math.max(0, pageCount - 1) }, (_, index) => index + 2).map(async (page) => (
      parse<{ items: PracticeCandidate[]; total: number }>(
        await fetch(`/api/practice-candidates?${candidateQuery(filters, page).toString()}`),
      )
    )),
  );
  return {
    items: [first, ...remaining].flatMap((result) => result.items),
    total: first.total,
  };
}

export async function createPracticeSession(payload: CreatePracticePayload): Promise<PracticeSession> {
  return parse<PracticeSession>(await fetchJson("/api/practice-sessions", "POST", payload));
}

export async function getPracticeSession(sessionId: string): Promise<PracticeSession> {
  return parse<PracticeSession>(await fetch(`/api/practice-sessions/${sessionId}`));
}

export async function getPracticeHistory(page = 1): Promise<PracticeHistoryPage> {
  return parse<PracticeHistoryPage>(
    await fetch(`/api/practice-sessions?page=${page}&page_size=20`),
  );
}

export async function savePracticeAnswer(sessionId: string, itemId: string, selectedAnswer: string | boolean): Promise<PracticeItem> {
  return parse<PracticeItem>(await fetchJson(`/api/practice-sessions/${sessionId}/answers`, "POST", {
    practice_item_id: itemId,
    selected_answer: selectedAnswer,
  }));
}

export async function submitPracticeSession(sessionId: string): Promise<PracticeResult> {
  return parse<PracticeResult>(await fetchJson(`/api/practice-sessions/${sessionId}/submit`, "POST"));
}

export async function getPracticeResult(sessionId: string): Promise<PracticeResult> {
  return parse<PracticeResult>(await fetch(`/api/practice-sessions/${sessionId}/result`));
}

function fetchJson(url: string, method: "POST", body?: unknown): Promise<Response> {
  return fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
}
