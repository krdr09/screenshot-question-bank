export type ReviewOption = { label: string; text: string; order_index?: number };
export type ReviewPart = {
  id: string;
  source_image_id: string;
  part_index: number;
  source_y_start: number;
  source_y_end: number;
  source_height: number;
  crop_url: string;
  source_url: string;
};
export type ReviewRelationship = {
  id: string;
  batch_id: string;
  batch_name: string;
  course_name: string;
  category_path: string;
  relation_type: string;
  primary_candidate_id: string;
  secondary_candidate_id: string;
  decision: string;
  confidence: number;
  evidence: Record<string, unknown>;
  status: string;
};
export type ReviewOrderingIssue = {
  id: string;
  batch_id: string;
  batch_name: string;
  course_name: string;
  category_path: string;
  code: string;
  message: string;
  image_ids: string[];
  status: string;
};
export type ReviewItem = {
  id: string;
  batch_id: string;
  batch_name: string;
  course_name: string;
  category_path: string;
  source_image_id: string;
  source_image_name: string;
  source_display_number: string | null;
  question_type: "single_choice" | "true_false";
  stem: string;
  options: ReviewOption[];
  correct_answer: string | boolean | null;
  true_label: string | null;
  false_label: string | null;
  explanation: string | null;
  raw_ocr_text: string;
  edited_ocr_text: string | null;
  ocr_confidence: number;
  parse_confidence: number;
  display_mode: string;
  anomaly_flags: Array<{ code: string; severity: string; message: string }>;
  status: string;
  duplicate_of_id: string | null;
  parts: ReviewPart[];
  relationships: ReviewRelationship[];
  can_undo: boolean;
};
export type ReviewBundle = {
  items: ReviewItem[];
  relationships: ReviewRelationship[];
  ordering_issues: ReviewOrderingIssue[];
};
export type ReviewBatchContextItem = {
  id: string;
  batch_id: string;
  stem: string;
  status: string;
};
export type ReviewBatchContext = {
  items: ReviewBatchContextItem[];
};

async function parse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(body?.detail ?? `请求失败（HTTP ${response.status}）`);
  }
  return response.json() as Promise<T>;
}

export async function getReviewItems(jobId: string | undefined, filters: Record<string, string>): Promise<ReviewBundle> {
  const query = new URLSearchParams();
  if (jobId) query.set("batch_id", jobId);
  for (const [key, value] of Object.entries(filters)) if (value) query.set(key, value);
  return parse<ReviewBundle>(await fetch(`/api/review-items?${query.toString()}`));
}

export async function getReviewItem(id: string): Promise<ReviewItem> {
  return parse<ReviewItem>(await fetch(`/api/review-items/${id}`));
}

export async function getReviewBatchContext(id: string): Promise<ReviewBatchContext> {
  return parse<ReviewBatchContext>(
    await fetch(`/api/review-items/${id}/batch-context`),
  );
}

function partUpdates(item: ReviewItem) {
  return item.parts.map(
    ({ id, part_index, source_y_start, source_y_end }) => ({
      id,
      part_index,
      source_y_start,
      source_y_end,
    }),
  );
}

export async function saveReviewItem(item: ReviewItem): Promise<ReviewItem> {
  return parse<ReviewItem>(await fetch(`/api/review-items/${item.id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      edited_ocr_text: item.edited_ocr_text,
      source_display_number: item.source_display_number,
      stem: item.stem,
      question_type: item.question_type,
      options: item.options,
      correct_answer: item.correct_answer,
      true_label: item.true_label,
      false_label: item.false_label,
      explanation: item.explanation,
      display_mode: item.display_mode,
      parts: partUpdates(item),
    }),
  }));
}

export async function saveReviewRelationshipCrops(
  relationshipId: string,
  primary: ReviewItem,
  secondary: ReviewItem,
): Promise<{ primary: ReviewItem; secondary: ReviewItem }> {
  return parse<{ primary: ReviewItem; secondary: ReviewItem }>(
    await fetch(`/api/review-relationships/${relationshipId}/crops`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        primary_parts: partUpdates(primary),
        secondary_parts: partUpdates(secondary),
      }),
    }),
  );
}

export async function deleteReviewItem(id: string): Promise<ReviewItem> {
  return parse<ReviewItem>(await fetch(`/api/review-items/${id}`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirmed: true }),
  }));
}

export async function mergeReviewItem(primaryId: string, secondaryId: string, relationshipId?: string): Promise<ReviewItem> {
  return parse<ReviewItem>(await fetch(`/api/review-items/${primaryId}/merge`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      secondary_candidate_id: secondaryId,
      ...(relationshipId ? { relationship_id: relationshipId } : {}),
      confirmed: true,
    }),
  }));
}

export async function postReviewAction(path: string, body?: Record<string, unknown>): Promise<unknown> {
  return parse(await fetch(path, {
    method: "POST",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  }));
}
