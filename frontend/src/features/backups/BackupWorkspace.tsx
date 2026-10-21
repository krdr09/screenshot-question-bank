import { useEffect, useRef, useState } from "react";

import {
  backupDownloadUrl,
  createBackup,
  listBackups,
  restoreBackup,
  validateBackup,
  type BackupDetail,
  type BackupRestoreResult,
  type BackupValidation,
} from "./api";


type Props = { onRestartRequired?: () => void };
type BusyAction = "create" | "validate" | "restore" | "refresh" | null;

function message(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  return `${(value / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

export function BackupWorkspace({ onRestartRequired }: Props) {
  const [backups, setBackups] = useState<BackupDetail[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [busy, setBusy] = useState<BusyAction>(null);
  const [selectedBackupId, setSelectedBackupId] = useState("");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [validation, setValidation] = useState<BackupValidation | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [restoreResult, setRestoreResult] = useState<BackupRestoreResult | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const sealed = restoreResult !== null;

  async function load(showBusy = false) {
    if (showBusy) setBusy("refresh");
    try {
      setBackups(await listBackups());
      setError("");
    } catch (reason) {
      setError(message(reason, "读取备份列表失败"));
    } finally {
      setListLoading(false);
      if (showBusy) setBusy(null);
    }
  }

  useEffect(() => { void load(); }, []);

  function clearValidation() {
    setValidation(null);
    setConfirmed(false);
    setRestoreResult(null);
    setNotice("");
    setError("");
  }

  function chooseExisting(backupId: string) {
    setSelectedBackupId(backupId);
    setSelectedFile(null);
    if (fileInput.current) fileInput.current.value = "";
    clearValidation();
  }

  function chooseFile(file: File | null) {
    setSelectedFile(file);
    if (file) setSelectedBackupId("");
    clearValidation();
  }

  async function create() {
    if (busy || sealed) return;
    setBusy("create");
    setError("");
    setNotice("");
    try {
      const created = await createBackup();
      setNotice(`备份创建成功：${created.filename}`);
      setBackups(await listBackups());
    } catch (reason) {
      setError(message(reason, "创建备份失败"));
    } finally {
      setBusy(null);
    }
  }

  async function validate() {
    if (busy || sealed || (!selectedBackupId && !selectedFile)) return;
    setBusy("validate");
    setError("");
    setNotice("");
    setValidation(null);
    setConfirmed(false);
    try {
      const result = selectedFile
        ? await validateBackup({ file: selectedFile })
        : await validateBackup({ backupId: selectedBackupId });
      setValidation(result);
    } catch (reason) {
      setError(message(reason, "验证备份失败"));
    } finally {
      setBusy(null);
    }
  }

  async function restore() {
    if (busy || sealed || !validation || !confirmed) return;
    if (!window.confirm("确认使用已验证的备份替换当前题库？系统会先创建安全备份，成功后需要重启应用。")) return;
    setBusy("restore");
    setError("");
    try {
      const result = await restoreBackup(validation.validation_token);
      setRestoreResult(result);
      onRestartRequired?.();
    } catch (reason) {
      setError(message(reason, "恢复失败，当前数据未被替换"));
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="backup-workspace" aria-labelledby="backup-workspace-title">
      <div className="backup-heading">
        <div><span className="progress-kicker">VERIFIED LOCAL BACKUP</span><h3 id="backup-workspace-title">备份与恢复</h3></div>
        <p>备份、验证和恢复均在本机完成，不会上传任何截图或题库数据。</p>
      </div>

      <div className="backup-toolbar">
        <button type="button" disabled={Boolean(busy) || sealed} onClick={create}>
          {busy === "create" ? "正在创建备份…" : "创建完整备份"}
        </button>
        <button type="button" disabled={Boolean(busy) || sealed} onClick={() => void load(true)}>
          {busy === "refresh" ? "正在刷新…" : "刷新列表"}
        </button>
      </div>

      {notice && <p className="backup-notice" role="status">{notice}</p>}
      {error && <p className="backup-error" role="alert">{error}</p>}

      <div className="backup-list" aria-label="本机备份列表">
        {listLoading ? <p role="status">正在读取备份列表…</p> : backups.length === 0 ? <p>尚未创建本机备份。</p> : backups.map((item) => (
          <article key={item.backup_id}>
            <label>
              <input
                type="radio"
                name="backup-source"
                checked={selectedBackupId === item.backup_id}
                disabled={Boolean(busy) || sealed}
                onChange={() => chooseExisting(item.backup_id)}
              />
              <span><strong>{item.filename}</strong><small>{formatDate(item.created_at)} · {formatBytes(item.size)}</small></span>
            </label>
            <span>{item.counts.questions} 道题目 · {item.counts.source_images} 张截图 · {item.counts.attempts} 次作答</span>
            {sealed ? (
              <span aria-disabled="true">下载</span>
            ) : (
              <a href={backupDownloadUrl(item.backup_id)} download={item.filename} aria-label={`下载 ${item.filename}`}>下载</a>
            )}
          </article>
        ))}
      </div>

      <fieldset className="backup-source" disabled={Boolean(busy) || sealed}>
        <legend>验证来源</legend>
        <label htmlFor="external-backup">选择外部 ZIP 备份</label>
        <input
          ref={fileInput}
          id="external-backup"
          type="file"
          accept=".zip,application/zip"
          onChange={(event) => chooseFile(event.target.files?.[0] ?? null)}
        />
        {selectedFile && <p>已选择 {selectedFile.name}</p>}
        <button type="button" disabled={Boolean(busy) || (!selectedBackupId && !selectedFile)} onClick={validate}>
          {busy === "validate" ? "正在验证…" : "验证备份"}
        </button>
      </fieldset>

      {validation && (
        <section className="backup-summary" aria-labelledby="backup-summary-title">
          <h4 id="backup-summary-title">验证摘要</h4>
          <p className="backup-hash">文件哈希全部通过</p>
          <dl>
            <div><dt>备份文件</dt><dd>{validation.filename}</dd></div>
            <div><dt>创建时间</dt><dd>{formatDate(validation.created_at)}</dd></div>
            <div><dt>应用 / 数据库版本</dt><dd>{validation.app_version} / {validation.schema_revision}</dd></div>
            <div><dt>文件与容量</dt><dd>{validation.file_count} 个文件 · {formatBytes(validation.uncompressed_bytes)}</dd></div>
            <div><dt>题目</dt><dd>{validation.counts.questions}</dd></div>
            <div><dt>截图 / 裁切图</dt><dd>{validation.counts.source_images} / {validation.counts.crop_images}</dd></div>
            <div><dt>作答 / 错题</dt><dd>{validation.counts.attempts} / {validation.counts.wrong_questions}</dd></div>
            <div><dt>数据库迁移</dt><dd>{validation.migration_required ? "恢复后需要数据库迁移" : "不需要迁移"}</dd></div>
          </dl>
          {validation.warnings.length > 0 && <ul className="backup-warnings">{validation.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>}

          <div className="backup-confirmation">
            <label>
              <input type="checkbox" checked={confirmed} disabled={Boolean(busy) || sealed} onChange={(event) => setConfirmed(event.target.checked)} />
              已了解恢复会替换当前题库，系统会先创建安全备份，完成后必须重启应用
            </label>
            <button type="button" disabled={!confirmed || Boolean(busy) || sealed} onClick={restore}>
              {busy === "restore" ? "正在安全恢复…" : "确认恢复"}
            </button>
          </div>
        </section>
      )}

      {restoreResult && (
        <section className="backup-restart" role="status">
          <h4>恢复已完成，请重启应用</h4>
          <p>恢复前安全备份已保留：<strong>{restoreResult.safety_backup.filename}</strong></p>
          <p>当前进程已停止接受题库操作。请关闭并重新启动应用后继续使用。</p>
        </section>
      )}
    </section>
  );
}
