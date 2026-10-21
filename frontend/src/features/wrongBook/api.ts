import type { PracticeOption } from "../practice/api";

export type WrongImagePart = { part_index: number; url: string };
export type WrongQuestion = {
  question_id: string;
  question_type: "single_choice" | "true_false";
  stem: string;
  options: PracticeOption[];
  correct_answer: boolean | string;
  explanation: string | null;
  course_id: string;
  course_name: string;
  category_id: string | null;
  category_path: string;
  batch_id: string;
  batch_name: string;
  batch_position: number;
  source_display_number: string | null;
  display_mode: string;
  error_count: number;
  mastered: boolean;
  image_parts: WrongImagePart[];
};

async function parse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(body?.detail ?? `请求失败（HTTP ${response.status}）`);
  }
  return response.json() as Promise<T>;
}

export async function getWrongQuestions(filters: Record<string, string | boolean>): Promise<WrongQuestion[]> {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value === "" || value === false) continue;
    query.set(key, String(value));
  }
  const suffix = query.size ? `?${query.toString()}` : "";
  return (await parse<{ items: WrongQuestion[] }>(await fetch(`/api/wrong-questions${suffix}`))).items;
}

async function changeMastery(questionId: string, action: "master" | "restore"): Promise<{ question_id: string; mastered: boolean }> {
  return parse(await fetch(`/api/wrong-questions/${questionId}/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  }));
}

export const masterWrongQuestion = (questionId: string) => changeMastery(questionId, "master");
export const restoreWrongQuestion = (questionId: string) => changeMastery(questionId, "restore");
