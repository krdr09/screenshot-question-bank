import { useEffect, useState } from "react";

import { getPracticeHistory, type PracticeHistoryPage } from "./api";

export function PracticeHistory({ onResume }: { onResume: (sessionId: string) => void }) {
  const [page, setPage] = useState(1);
  const [history, setHistory] = useState<PracticeHistoryPage | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    setError("");
    void getPracticeHistory(page).then(
      (value) => {
        if (active) setHistory(value);
      },
      (reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "练习历史加载失败");
      },
    );
    return () => {
      active = false;
    };
  }, [page]);

  const pageCount = history ? Math.max(1, Math.ceil(history.total / history.page_size)) : 1;
  return (
    <section className="practice-history" aria-labelledby="practice-history-title">
      <div className="practice-heading">
        <div><p className="eyebrow">PRACTICE HISTORY</p><h3 id="practice-history-title">练习历史</h3></div>
        {history && <span>共 {history.total} 次</span>}
      </div>
      {!history && !error && <p>正在加载练习历史…</p>}
      {error && <p role="alert">{error}</p>}
      {history?.items.length === 0 && <p>还没有历史练习。完成或保存一次刷题后会显示在这里。</p>}
      {!!history?.items.length && (
        <div className="practice-history-list" aria-label="练习历史列表">
          {history.items.map((item) => (
            <article key={item.id}>
              <div>
                <strong>{new Date(item.started_at).toLocaleString("zh-CN")}</strong>
                <span>{item.feedback_mode === "immediate" ? "逐题反馈" : "整套反馈"} · {item.order_mode === "random" ? "随机" : "顺序"} · {item.actual_count} 题</span>
                <p>{item.status === "active"
                  ? `进行中 · 已答 ${item.answered_count}/${item.actual_count}`
                  : `已提交 · 正确 ${item.correct_count ?? 0} · 错误 ${item.incorrect_count ?? 0} · 未答 ${item.unanswered_count ?? 0}`}</p>
              </div>
              <button type="button" onClick={() => onResume(item.id)}>{item.status === "active" ? "继续练习" : "查看结果"}</button>
            </article>
          ))}
        </div>
      )}
      {history && pageCount > 1 && (
        <nav className="practice-history-pages" aria-label="练习历史分页">
          <button type="button" disabled={page === 1} onClick={() => setPage((value) => value - 1)}>上一页</button>
          <span>第 {page} / {pageCount} 页</span>
          <button type="button" disabled={page === pageCount} onClick={() => setPage((value) => value + 1)}>下一页</button>
        </nav>
      )}
    </section>
  );
}
