export type BackupCounts = {
  questions: number;
  source_images: number;
  crop_images: number;
  attempts: number;
  wrong_questions: number;
};

export type BackupDetail = {
  backup_id: string;
  filename: string;
  created_at: string;
  size: number;
  app_version: string;
  schema_revision: string;
  reason: "manual" | "pre_restore";
  counts: BackupCounts;
};

export type BackupListResponse = { items: BackupDetail[] };

export type BackupValidation = {
  validation_token: string;
  backup_id: string;
  filename: string;
  created_at: string;
  app_version: string;
  schema_revision: string;
  file_count: number;
  uncompressed_bytes: number;
  counts: BackupCounts;
  hashes_verified: true;
  migration_required: boolean;
  warnings: string[];
};

export type BackupRestoreResult = {
  restored_backup_id: string;
  safety_backup: BackupDetail;
  restart_required: true;
};

async function parse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
    const detail = typeof body?.detail === "string" ? body.detail : null;
    throw new Error(detail ?? `请求失败（HTTP ${response.status}）`);
  }
  return response.json() as Promise<T>;
}

export async function listBackups(): Promise<BackupDetail[]> {
  return (await parse<BackupListResponse>(await fetch("/api/backups"))).items;
}

export async function createBackup(): Promise<BackupDetail> {
  return parse<BackupDetail>(await fetch("/api/backups", { method: "POST" }));
}

export async function validateBackup(source: { backupId: string } | { file: File }): Promise<BackupValidation> {
  const body = new FormData();
  if ("backupId" in source) body.append("backup_id", source.backupId);
  else body.append("file", source.file);
  return parse<BackupValidation>(await fetch("/api/backups/validate", { method: "POST", body }));
}

export async function restoreBackup(validationToken: string): Promise<BackupRestoreResult> {
  return parse<BackupRestoreResult>(await fetch("/api/backups/restore", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ validation_token: validationToken, confirmed: true }),
  }));
}

export function backupDownloadUrl(backupId: string): string {
  return `/api/backups/${encodeURIComponent(backupId)}/content`;
}
