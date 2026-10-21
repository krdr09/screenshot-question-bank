import { useEffect, useState } from "react";

import {
  cancelImport,
  getImportProgress,
  type ImportJobProgress,
} from "./api";
import { OrderingReview } from "../ordering/OrderingReview";
import { ReviewWorkspace } from "../review/ReviewWorkspace";


const terminalStatuses = new Set([
  "ordering_review",
  "question_review",
  "ready",
  "partially_ready",
  "failed",
  "cancelled",
]);

const statusLabels: Record<string, string> = {
  created: "任务已创建",
  uploading: "正在上传",
  queued: "等待处理",
  processing: "正在 OCR",
  ordering_review: "等待顺序检查",
  question_review: "等待题目检查",
  ready: "处理完成",
  partially_ready: "部分完成",
  failed: "处理失败",
  cancelled: "已取消",
};

function deviceLabel(device: string | null): string {
  if (device === "mock") return "Mock OCR";
  if (device === "gpu") return "GPU";
  if (device === "cpu") return "CPU";
  return "等待 OCR 引擎";
}

export function ImportProgress({ initial }: { initial: ImportJobProgress }) {
  const [job, setJob] = useState(initial);
  const [requestError, setRequestError] = useState("");

  useEffect(() => {
    if (terminalStatuses.has(job.status)) return;
    const timer = window.setInterval(async () => {
      try {
        setJob(await getImportProgress(job.id));
        setRequestError("");
      } catch (error) {
        setRequestError(error instanceof Error ? error.message : "读取进度失败");
      }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [job.id, job.status]);

  async function cancel() {
    try {
      setJob(await cancelImport(job.id));
      setRequestError("");
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : "取消任务失败");
    }
  }

  const canCancel = ["queued", "processing"].includes(job.status) && !job.cancel_requested;
  return (
    <article className="progress-card" aria-label={`导入任务 ${job.id}`}>
      <div className="progress-heading">
        <div>
          <span className="progress-kicker">OCR TASK</span>
          <h3>{statusLabels[job.status] ?? job.status}</h3>
        </div>
        <strong>{job.processed_count} / {job.uploaded_count}</strong>
      </div>
      <progress value={job.processed_count} max={Math.max(job.uploaded_count, 1)}>
        {job.processed_count} / {job.uploaded_count}
      </progress>
      <div className="progress-metrics">
        <span>已找到 {job.found_question_count}</span>
        <span>高置信度 {job.high_confidence_count}</span>
        <span>待检查 {job.review_count}</span>
        <span>错误 {job.error_count}</span>
      </div>
      <dl className="progress-detail">
        <div><dt>处理设备</dt><dd>{deviceLabel(job.ocr_device)}</dd></div>
        <div><dt>当前文件</dt><dd>{job.current_file ?? "—"}</dd></div>
      </dl>
      {job.error_log.length > 0 && (
        <details className="progress-errors">
          <summary>查看错误日志（{job.error_log.length}）</summary>
          <ul>{job.error_log.map((error) => <li key={`${error.file}-${error.message}`}>{error.file}：{error.message}</li>)}</ul>
        </details>
      )}
      {requestError && <p className="import-warning" role="alert">{requestError}</p>}
      {canCancel && <button type="button" className="cancel-task" onClick={cancel}>取消此任务</button>}
      {job.status === "ordering_review" && (
        <OrderingReview
          jobId={job.id}
          onConfirmed={async () => setJob(await getImportProgress(job.id))}
        />
      )}
      {job.status === "question_review" && (
        <ReviewWorkspace
          jobId={job.id}
          onChanged={async () => setJob(await getImportProgress(job.id))}
        />
      )}
    </article>
  );
}
