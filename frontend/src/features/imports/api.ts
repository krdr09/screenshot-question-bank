import type { ImportMapping } from "./folderMapping";

type ImportJob = {
  id: string;
};

export type ImportJobProgress = {
  id: string;
  status: string;
  uploaded_count: number;
  processed_count: number;
  found_question_count: number;
  high_confidence_count: number;
  review_count: number;
  error_count: number;
  current_file: string | null;
  ocr_device: string | null;
  cancel_requested: boolean;
  error_log: Array<{ file: string; message: string }>;
};

async function parseResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(body?.detail ?? `请求失败（HTTP ${response.status}）`);
  }
  return (await response.json()) as T;
}

export async function submitImportMapping(
  mapping: ImportMapping,
): Promise<ImportJobProgress> {
  const created = await parseResponse<ImportJob>(
    await fetch("/api/import-jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        course_name: mapping.courseName,
        category_path: mapping.categoryPath,
        batch_name: mapping.batchName,
        source_relative_path: mapping.sourceRelativePath || mapping.batchName,
      }),
    }),
  );

  const form = new FormData();
  for (const entry of mapping.files) {
    form.append("files", entry.file, entry.file.name);
    form.append("relative_paths", entry.relativePath);
  }
  await parseResponse(
    await fetch(`/api/import-jobs/${created.id}/files`, {
      method: "POST",
      body: form,
    }),
  );
  return parseResponse<ImportJobProgress>(
    await fetch(`/api/import-jobs/${created.id}/process`, { method: "POST" }),
  );
}

export async function getImportProgress(id: string): Promise<ImportJobProgress> {
  return parseResponse<ImportJobProgress>(await fetch(`/api/import-jobs/${id}`));
}

export async function cancelImport(id: string): Promise<ImportJobProgress> {
  return parseResponse<ImportJobProgress>(
    await fetch(`/api/import-jobs/${id}/cancel`, { method: "POST" }),
  );
}
