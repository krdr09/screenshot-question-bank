export type ReviewFlag = "duplicate" | "unrelated" | "damaged" | null;

export type OrderingImage = {
  id: string;
  original_name: string;
  order_index: number;
  review_flag: ReviewFlag;
  duplicate_of_id: string | null;
  content_url: string;
};

export type ScreenshotOverlap = {
  source_image_id: string;
  target_image_id: string;
  text_score: number;
  visual_score: number;
  combined_score: number;
  overlap_ratio: number;
  matched_text: string[];
};

export type OrderingIssue = {
  id: string;
  code: string;
  message: string;
  image_ids: string[];
  status: string;
};

export type OrderingData = {
  batch_id: string;
  status: string;
  requires_review: boolean;
  images: OrderingImage[];
  overlaps: ScreenshotOverlap[];
  issues: OrderingIssue[];
};

async function parse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(body?.detail ?? `请求失败（HTTP ${response.status}）`);
  }
  return (await response.json()) as T;
}

export async function getOrdering(jobId: string): Promise<OrderingData> {
  return parse<OrderingData>(await fetch(`/api/import-jobs/${jobId}/ordering`));
}

export async function saveOrdering(
  jobId: string,
  images: OrderingImage[],
): Promise<OrderingData> {
  return parse<OrderingData>(
    await fetch(`/api/import-jobs/${jobId}/ordering`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        items: images.map((image) => ({
          image_id: image.id,
          review_flag: image.review_flag,
        })),
      }),
    }),
  );
}
