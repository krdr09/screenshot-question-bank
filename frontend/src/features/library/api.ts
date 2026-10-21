export type LibraryOption = { label: string; text: string; order_index?: number };
export type LibraryPart = {
  id: string;
  source_image_id: string;
  part_index: number;
  source_y_start: number;
  source_y_end: number;
  crop_url: string;
  source_url: string;
};
export type LibraryQuestion = {
  id: string;
  internal_uuid: string;
  batch_id: string;
  batch_name: string;
  course_id: string;
  course_name: string;
  category_id: string | null;
  category_path: string;
  source_image_id: string;
  source_image_name: string;
  source_display_number: string | null;
  question_type: "single_choice" | "true_false";
  stem: string;
  options: LibraryOption[];
  correct_answer: string | boolean | null;
  true_label: string | null;
  false_label: string | null;
  explanation: string | null;
  raw_ocr_text: string;
  edited_ocr_text: string | null;
  display_mode: string;
  status: string;
  in_wrong_book: boolean;
  parts: LibraryPart[];
};
export type CourseSummary = { id: string; name: string; categories: Array<{ id: string; path: string }> };
export type BatchSummary = { id: string; name: string; status: string; course_id: string; course_name: string; category_id: string | null; category_path: string; question_count: number };
export type LibraryPage = { items: LibraryQuestion[]; total: number; page: number; page_size: number };
export type BatchDeleteResult = {
  id: string;
  name: string;
  deleted: boolean;
  deleted_questions: number;
  deleted_images: number;
  cleanup_pending: boolean;
};

async function parse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(body?.detail ?? `请求失败（HTTP ${response.status}）`);
  }
  return response.json() as Promise<T>;
}

export async function getCourses(): Promise<CourseSummary[]> {
  return (await parse<{ items: CourseSummary[] }>(await fetch("/api/courses"))).items;
}

export async function getBatches(courseId = "", categoryId = ""): Promise<BatchSummary[]> {
  const query = new URLSearchParams();
  if (courseId) query.set("course_id", courseId);
  if (categoryId) query.set("category_id", categoryId);
  const suffix = query.size ? `?${query.toString()}` : "";
  return (await parse<{ items: BatchSummary[] }>(await fetch(`/api/batches${suffix}`))).items;
}

export async function getQuestions(
  filters: Record<string, string>,
  page = 1,
  pageSize = 50,
): Promise<LibraryPage> {
  const query = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  for (const [key, value] of Object.entries(filters)) if (value) query.set(key, value);
  return parse<LibraryPage>(await fetch(`/api/questions?${query.toString()}`));
}

export async function saveQuestion(question: LibraryQuestion): Promise<LibraryQuestion> {
  return parse<LibraryQuestion>(await fetch(`/api/questions/${question.id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      category_id: question.category_id,
      source_display_number: question.source_display_number,
      stem: question.stem,
      question_type: question.question_type,
      options: question.options,
      correct_answer: question.correct_answer,
      true_label: question.true_label,
      false_label: question.false_label,
      display_mode: question.display_mode,
    }),
  }));
}

async function confirmedRequest<T>(url: string, method: "POST" | "DELETE"): Promise<T> {
  return parse<T>(await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirmed: true }),
  }));
}

export function rerunQuestion(questionId: string): Promise<LibraryQuestion> {
  return confirmedRequest(`/api/questions/${questionId}/rerun-ocr`, "POST");
}

export function deleteQuestion(questionId: string): Promise<{ id: string; deleted: boolean }> {
  return confirmedRequest(`/api/questions/${questionId}`, "DELETE");
}

export function deleteBatch(batchId: string): Promise<BatchDeleteResult> {
  return confirmedRequest(`/api/batches/${batchId}`, "DELETE");
}
