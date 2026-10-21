import { useEffect, useMemo, useState } from "react";

import {
  getOrdering,
  saveOrdering,
  type OrderingData,
  type OrderingImage,
  type ReviewFlag,
} from "./api";


export function OrderingReview({
  jobId,
  onConfirmed,
}: {
  jobId: string;
  onConfirmed?: () => void;
}) {
  const [data, setData] = useState<OrderingData | null>(null);
  const [images, setImages] = useState<OrderingImage[]>([]);
  const [compareIndex, setCompareIndex] = useState(0);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let active = true;
    setError("");
    getOrdering(jobId)
      .then((next) => {
        if (!active) return;
        setData(next);
        setImages(next.images);
        setCompareIndex(0);
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "读取截图顺序失败");
      });
    return () => {
      active = false;
    };
  }, [jobId]);

  const pair = images.length > 1
    ? [images[Math.min(compareIndex, images.length - 2)], images[Math.min(compareIndex, images.length - 2) + 1]] as const
    : null;
  const overlap = useMemo(() => {
    if (!pair || !data) return null;
    return data.overlaps.find(
      (item) => item.source_image_id === pair[0].id && item.target_image_id === pair[1].id,
    ) ?? null;
  }, [data, pair]);

  function move(index: number, offset: number) {
    const target = index + offset;
    if (target < 0 || target >= images.length) return;
    setImages((current) => {
      const next = [...current];
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
    setCompareIndex(Math.max(0, Math.min(index, target, images.length - 2)));
  }

  function drop(sourceId: string, targetId: string) {
    if (!sourceId || sourceId === targetId) return;
    setImages((current) => {
      const sourceIndex = current.findIndex((item) => item.id === sourceId);
      const targetIndex = current.findIndex((item) => item.id === targetId);
      if (sourceIndex < 0 || targetIndex < 0) return current;
      const next = [...current];
      const [moved] = next.splice(sourceIndex, 1);
      next.splice(targetIndex, 0, moved);
      return next;
    });
  }

  function setFlag(imageId: string, reviewFlag: ReviewFlag) {
    setImages((current) => current.map((item) =>
      item.id === imageId ? { ...item, review_flag: reviewFlag } : item,
    ));
  }

  async function confirm() {
    if (saving) return;
    setSaving(true);
    try {
      const saved = await saveOrdering(jobId, images);
      setData(saved);
      setImages(saved.images);
      setError("");
      onConfirmed?.();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存截图顺序失败");
    } finally {
      setSaving(false);
    }
  }

  if (error && !data) return <p className="import-warning" role="alert">{error}</p>;
  if (!data) return <p className="ordering-loading" role="status">正在读取截图顺序…</p>;

  return (
    <section className="ordering-review" aria-labelledby={`ordering-title-${jobId}`}>
      <div className="ordering-heading">
        <div>
          <span className="progress-kicker">ORDER REVIEW</span>
          <h4 id={`ordering-title-${jobId}`}>截图顺序检查</h4>
        </div>
        <p>系统没有足够证据自动决定顺序。请核对重叠区域后保存。</p>
      </div>

      <ol className="ordering-sequence">
        {images.map((image, index) => (
          <li
            key={image.id}
            draggable
            onDragStart={(event) => event.dataTransfer.setData("text/plain", image.id)}
            onDragOver={(event) => event.preventDefault()}
            onDrop={(event) => {
              event.preventDefault();
              drop(event.dataTransfer.getData("text/plain"), image.id);
            }}
          >
            <img src={image.content_url} alt={`缩略图 ${image.original_name}`} />
            <strong>{image.original_name}</strong>
            <div className="ordering-controls">
              <button type="button" aria-label={`上移 ${image.original_name}`} disabled={index === 0} onClick={() => move(index, -1)}>↑</button>
              <button type="button" aria-label={`下移 ${image.original_name}`} disabled={index === images.length - 1} onClick={() => move(index, 1)}>↓</button>
            </div>
            <label>
              <span>图片标记</span>
              <select
                aria-label={`标记 ${image.original_name}`}
                value={image.review_flag ?? ""}
                onChange={(event) => setFlag(image.id, (event.target.value || null) as ReviewFlag)}
              >
                <option value="">正常</option>
                <option value="duplicate">重复</option>
                <option value="unrelated">无关</option>
                <option value="damaged">损坏</option>
              </select>
            </label>
          </li>
        ))}
      </ol>

      {images.length > 1 && (
        <div className="pair-buttons" aria-label="相邻截图对比选择">
          {images.slice(0, -1).map((image, index) => (
            <button key={`${image.id}-${images[index + 1].id}`} type="button" onClick={() => setCompareIndex(index)} aria-label={`对比 ${image.original_name} 和 ${images[index + 1].original_name}`}>
              {index + 1} ↔ {index + 2}
            </button>
          ))}
        </div>
      )}

      {pair && (
        <section className="overlap-comparison" aria-label="重叠对比">
          <div><img src={pair[0].content_url} alt={`对比图 ${pair[0].original_name}`} /><span>{pair[0].original_name}</span></div>
          <div><img src={pair[1].content_url} alt={`对比图 ${pair[1].original_name}`} /><span>{pair[1].original_name}</span></div>
          <dl>
            <div><dt>综合</dt><dd>{overlap ? `${Math.round(overlap.combined_score * 100)}%` : "无可靠匹配"}</dd></div>
            <div><dt>文字</dt><dd>{overlap ? `${Math.round(overlap.text_score * 100)}%` : "—"}</dd></div>
            <div><dt>图像</dt><dd>{overlap ? `${Math.round(overlap.visual_score * 100)}%` : "—"}</dd></div>
          </dl>
        </section>
      )}

      {data.issues.length > 0 && (
        <ul className="ordering-issues">
          {data.issues.map((issue) => <li key={issue.id}>{issue.message}</li>)}
        </ul>
      )}
      {error && <p className="import-warning" role="alert">{error}</p>}
      <button className="confirm-order" type="button" disabled={saving} onClick={confirm}>
        {saving ? "正在保存…" : "确认并保存顺序"}
      </button>
    </section>
  );
}
