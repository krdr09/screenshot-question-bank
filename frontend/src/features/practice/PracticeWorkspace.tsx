import { useEffect, useMemo, useRef, useState } from "react";

import { getBatches, getCourses, type BatchSummary, type CourseSummary } from "../library/api";
import {
  createPracticeSession,
  getPracticeResult,
  getPracticeCandidates,
  getPracticeSession,
  savePracticeAnswer,
  submitPracticeSession,
  type PracticeCandidate,
  PracticeApiError,
  type PracticeItem,
  type PracticeResult,
  type PracticeSession,
} from "./api";
import { answerText } from "./answerText";
import { QuestionImageViewer } from "./QuestionImageViewer";


type Screen = "settings" | "practice" | "result";
type Props = { initialQuestionIds?: string[]; onOpenWrongBook?: () => void };
export const LAST_PRACTICE_SESSION_KEY = "local-question-bank:last-practice-session";

const isImagePreferredMode = (displayMode: string) => (
  displayMode === "image_preferred" || displayMode === "image_first"
);

export function PracticeWorkspace({ initialQuestionIds = [], onOpenWrongBook }: Props) {
  const [screen, setScreen] = useState<Screen>("settings");
  const [session, setSession] = useState<PracticeSession | null>(null);
  const [result, setResult] = useState<PracticeResult | null>(null);
  const [courses, setCourses] = useState<CourseSummary[]>([]);
  const [batches, setBatches] = useState<BatchSummary[]>([]);
  const [candidates, setCandidates] = useState<PracticeCandidate[]>([]);
  const [candidateTotal, setCandidateTotal] = useState(0);
  const [batchPositionRange, setBatchPositionRange] = useState<{ min: number; max: number } | null>(null);
  const [courseId, setCourseId] = useState("");
  const [categoryId, setCategoryId] = useState("");
  const [batchId, setBatchId] = useState("");
  const [questionType, setQuestionType] = useState<"" | "single_choice" | "true_false">("");
  const [membership, setMembership] = useState<"all" | "wrong" | "unanswered">("all");
  const [selectionMode, setSelectionMode] = useState<"filter" | "manual">(initialQuestionIds.length ? "manual" : "filter");
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set(initialQuestionIds));
  const [countMode, setCountMode] = useState<"all" | "custom">("all");
  const [count, setCount] = useState("5");
  const [positionStart, setPositionStart] = useState("");
  const [positionEnd, setPositionEnd] = useState("");
  const [orderMode, setOrderMode] = useState<"sequential" | "random">("sequential");
  const [feedbackMode, setFeedbackMode] = useState<"immediate" | "deferred">("immediate");
  const [currentIndex, setCurrentIndex] = useState(0);
  const [draftAnswers, setDraftAnswers] = useState<Record<string, string | boolean>>({});
  const [restoring, setRestoring] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const candidateLoadVersion = useRef(0);
  const batchRangeLoadVersion = useRef(0);

  useEffect(() => {
    void getCourses().then(setCourses).catch((value: Error) => setError(value.message));
  }, []);
  useEffect(() => {
    void getBatches(courseId, categoryId).then(setBatches).catch((value: Error) => setError(value.message));
  }, [courseId, categoryId]);
  useEffect(() => {
    const version = ++candidateLoadVersion.current;
    const filters = {
      ...(courseId ? { course_id: courseId } : {}),
      ...(categoryId ? { category_id: categoryId } : {}),
      ...(batchId ? { batch_id: batchId } : {}),
      ...(questionType ? { question_type: questionType } : {}),
      membership,
      ...(selectionMode === "filter" && batchId && positionStart && positionEnd
        ? { position_start: Number(positionStart), position_end: Number(positionEnd) }
        : {}),
    };
    void getPracticeCandidates(filters).then((result) => {
      if (version !== candidateLoadVersion.current) return;
      setCandidates(result.items);
      setCandidateTotal(result.total);
    }).catch((value: Error) => {
      if (version === candidateLoadVersion.current) setError(value.message);
    });
  }, [courseId, categoryId, batchId, questionType, membership, selectionMode, positionStart, positionEnd]);
  useEffect(() => {
    const version = ++batchRangeLoadVersion.current;
    if (!batchId) {
      setBatchPositionRange(null);
      return;
    }
    void getPracticeCandidates({ batch_id: batchId, membership: "all" }).then((result) => {
      if (version !== batchRangeLoadVersion.current) return;
      const positions = result.items.map((item) => item.batch_position);
      setBatchPositionRange(positions.length ? { min: Math.min(...positions), max: Math.max(...positions) } : null);
    }).catch((value: Error) => {
      if (version === batchRangeLoadVersion.current) setError(value.message);
    });
  }, [batchId]);
  useEffect(() => {
    if (initialQuestionIds.length) return;
    const sessionId = localStorage.getItem(LAST_PRACTICE_SESSION_KEY);
    if (!sessionId) return;
    setRestoring(true);
    setError("");
    void getPracticeSession(sessionId).then(async (restored) => {
      setSession(restored);
      setCurrentIndex(Math.max(0, restored.items.findIndex((item) => !item.locked && !item.saved)));
      if (restored.status === "submitted") {
        setResult(await getPracticeResult(sessionId));
        setScreen("result");
      } else {
        setScreen("practice");
      }
    }).catch((value: Error) => {
      if (value instanceof PracticeApiError && value.status === 404) {
        localStorage.removeItem(LAST_PRACTICE_SESSION_KEY);
      }
      setError(`无法恢复上次刷题：${value.message}`);
      setScreen("settings");
    }).finally(() => setRestoring(false));
  }, []);
  useEffect(() => {
    if (!initialQuestionIds.length) return;
    setScreen("settings");
    setSelectionMode("manual");
    setSelectedIds(new Set(initialQuestionIds));
  }, [initialQuestionIds.join("|")]);

  const categories = useMemo(() => courses.find((course) => course.id === courseId)?.categories ?? [], [courses, courseId]);
  const availableCount = selectionMode === "manual" ? selectedIds.size : candidateTotal;
  const current = session?.items[currentIndex] ?? null;
  const currentAnswer = current && Object.prototype.hasOwnProperty.call(draftAnswers, current.id)
    ? draftAnswers[current.id]
    : current?.selected_answer;
  const hasOnePosition = Boolean(positionStart) !== Boolean(positionEnd);
  const numericPositionInvalid = Boolean(positionStart && positionEnd) && (
    !Number.isInteger(Number(positionStart))
    || !Number.isInteger(Number(positionEnd))
    || Number(positionStart) < 1
    || Number(positionEnd) < Number(positionStart)
  );
  const positionInvalid = selectionMode === "filter" && Boolean(batchId) && (hasOnePosition || numericPositionInvalid);

  const toggleSelected = (id: string) => setSelectedIds((currentIds) => {
    const next = new Set(currentIds);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  const start = async () => {
    setBusy(true);
    setError("");
    try {
      const selection = selectionMode === "manual"
        ? { manual_question_ids: [...selectedIds] }
        : {
            ...(courseId ? { course_id: courseId } : {}),
            ...(categoryId ? { category_id: categoryId } : {}),
            ...(batchId ? { batch_id: batchId } : {}),
            ...(questionType ? { question_type: questionType } : {}),
            membership,
            ...(batchId && positionStart && positionEnd ? { position_start: Number(positionStart), position_end: Number(positionEnd) } : {}),
          };
      const created = await createPracticeSession({
        selection,
        ...(countMode === "custom" ? { count: Number(count) } : {}),
        order_mode: orderMode,
        feedback_mode: feedbackMode,
      });
      setSession(created);
      localStorage.setItem(LAST_PRACTICE_SESSION_KEY, created.id);
      setDraftAnswers({});
      setCurrentIndex(0);
      setScreen("practice");
    } catch (value) {
      setError((value as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const answer = async (value: string | boolean) => {
    if (!session || !current || current.locked || session.status === "submitted") return;
    setBusy(true);
    setError("");
    try {
      const updated = await savePracticeAnswer(session.id, current.id, value);
      setDraftAnswers((answers) => {
        const next = { ...answers };
        delete next[current.id];
        return next;
      });
      setSession({
        ...session,
        answered_count: session.answered_count + (current.saved ? 0 : 1),
        items: session.items.map((item) => item.id === updated.id ? updated : item),
      });
    } catch (value) {
      setError((value as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const chooseAnswer = (value: string | boolean) => {
    if (!session || !current || current.locked || session.status === "submitted") return;
    setDraftAnswers((answers) => ({ ...answers, [current.id]: value }));
    if (session.feedback_mode === "deferred") void answer(value);
  };

  const submit = async () => {
    if (!session) return;
    const unanswered = session.items.filter((item) => !item.saved).length;
    if (!window.confirm(`确认提交整套答案？当前有 ${unanswered} 题未作答。`)) return;
    setBusy(true);
    setError("");
    try {
      setResult(await submitPracticeSession(session.id));
      setScreen("result");
    } catch (value) {
      setError((value as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (restoring) return (
    <section className="practice-workspace" aria-live="polite"><p>正在恢复上次刷题…</p></section>
  );

  if (screen === "settings") return (
    <section className="practice-workspace" aria-labelledby="practice-settings-title">
      <div className="practice-heading">
        <div><p className="eyebrow">PRACTICE SETTINGS</p><h2 id="practice-settings-title">刷题设置</h2></div>
        {onOpenWrongBook && <button type="button" onClick={onOpenWrongBook}>打开错题本</button>}
      </div>
      {error && <p role="alert" className="form-error">{error}</p>}
      <div className="practice-settings-grid">
        <label>课程<select aria-label="课程" value={courseId} onChange={(event) => { setCourseId(event.target.value); setCategoryId(""); setBatchId(""); }}><option value="">全部课程</option>{courses.map((course) => <option value={course.id} key={course.id}>{course.name}</option>)}</select></label>
        <label>分类/章节<select aria-label="分类/章节" value={categoryId} onChange={(event) => { setCategoryId(event.target.value); setBatchId(""); }}><option value="">全部分类</option>{categories.map((category) => <option value={category.id} key={category.id}>{category.path}</option>)}</select></label>
        <label>导入批次<select aria-label="导入批次" value={batchId} onChange={(event) => setBatchId(event.target.value)}><option value="">全部批次</option>{batches.map((batch) => <option value={batch.id} key={batch.id}>{batch.name}</option>)}</select></label>
        <label>题型<select aria-label="题型" value={questionType} onChange={(event) => setQuestionType(event.target.value as typeof questionType)}><option value="">全部题型</option><option value="single_choice">单选题</option><option value="true_false">判断题</option></select></label>
        <fieldset className="practice-count"><legend>题目数量</legend>
          <label><input aria-label="全部题目" type="radio" name="count-mode" checked={countMode === "all"} onChange={() => setCountMode("all")} />全部</label>
          <label><input aria-label="自定义数量" type="radio" name="count-mode" checked={countMode === "custom"} onChange={() => setCountMode("custom")} />自定义</label>
          <input aria-label="自定义题目数量" type="number" min="1" disabled={countMode === "all"} value={count} onChange={(event) => setCount(event.target.value)} />
        </fieldset>
        <label>起始位置<input aria-label="起始位置" type="number" min="1" disabled={!batchId || selectionMode === "manual"} value={positionStart} onChange={(event) => setPositionStart(event.target.value)} /></label>
        <label>结束位置<input aria-label="结束位置" type="number" min="1" disabled={!batchId || selectionMode === "manual"} value={positionEnd} onChange={(event) => setPositionEnd(event.target.value)} /></label>
        {batchPositionRange && <p className="practice-position-range">可用内部位置 {batchPositionRange.min}–{batchPositionRange.max}</p>}
      </div>
      {selectionMode === "filter" && batchId && hasOnePosition && <p className="form-error">请同时填写起始位置和结束位置。</p>}
      {selectionMode === "filter" && batchId && numericPositionInvalid && !hasOnePosition && <p className="form-error">内部位置必须是有效的正整数范围。</p>}
      <fieldset><legend>题目范围</legend>
        <label><input type="radio" name="membership" checked={membership === "all"} onChange={() => setMembership("all")} />全部正式题</label>
        <label><input type="radio" name="membership" checked={membership === "wrong"} onChange={() => setMembership("wrong")} />只刷错题</label>
        <label><input aria-label="只刷未做题" type="radio" name="membership" checked={membership === "unanswered"} onChange={() => setMembership("unanswered")} />只刷未做题</label>
      </fieldset>
      <fieldset><legend>选择方式</legend>
        <label><input type="radio" name="selection" checked={selectionMode === "filter"} onChange={() => setSelectionMode("filter")} />按筛选结果</label>
        <label><input type="radio" name="selection" checked={selectionMode === "manual"} onChange={() => setSelectionMode("manual")} />手动选题</label>
      </fieldset>
      <fieldset><legend>出题顺序</legend>
        <label><input aria-label="顺序出题" type="radio" name="order" checked={orderMode === "sequential"} onChange={() => setOrderMode("sequential")} />顺序出题</label>
        <label><input type="radio" name="order" checked={orderMode === "random"} onChange={() => setOrderMode("random")} />随机出题</label>
      </fieldset>
      <fieldset><legend>反馈方式</legend>
        <label><input aria-label="逐题即时反馈" type="radio" name="feedback" checked={feedbackMode === "immediate"} onChange={() => setFeedbackMode("immediate")} />逐题即时反馈</label>
        <label><input aria-label="整套完成后反馈" type="radio" name="feedback" checked={feedbackMode === "deferred"} onChange={() => setFeedbackMode("deferred")} />整套完成后反馈</label>
      </fieldset>
      <div className="practice-candidates" aria-label="候选题预览">
        <p>当前匹配 {candidateTotal} 题{selectionMode === "manual" ? `，已选 ${selectedIds.size} 题` : ""}</p>
        {candidates.map((candidate) => <label key={candidate.id}>
          <input type="checkbox" checked={selectedIds.has(candidate.id)} disabled={selectionMode !== "manual"} onChange={() => toggleSelected(candidate.id)} />
          <span>{candidate.stem} · 批次位置 {candidate.batch_position}{candidate.has_answered ? " · 已做" : " · 未做"}</span>
        </label>)}
      </div>
      {availableCount === 0 && <p className="form-error">没有符合当前选择的正式题，无法开始刷题。</p>}
      <button type="button" className="primary-action" disabled={busy || availableCount === 0 || positionInvalid || (countMode === "custom" && (!Number.isInteger(Number(count)) || Number(count) < 1))} onClick={() => void start()}>{busy ? "正在创建…" : "开始本次刷题"}</button>
    </section>
  );

  if (screen === "result" && result) return (
    <section className="practice-workspace" aria-labelledby="practice-result-title">
      <h2 id="practice-result-title">本次结果</h2>
      <div className="practice-result-summary" data-testid="practice-result-summary">
        <span>总题数 {result.total_count}</span><span>正确 {result.correct_count}</span><span>错误 {result.incorrect_count}</span><span>未作答 {result.unanswered_count}</span><span>正确率 {result.accuracy.toFixed(2)}%</span>
      </div>
      <div className="practice-result-list">{result.items.map((item) => (
        <article key={item.id}>
          <div><strong>{item.order_index + 1}. {item.stem}</strong><span className="practice-result-state">{item.was_unanswered ? "未作答" : item.is_correct ? "正确" : "错误"}</span></div>
          <p>你的答案：{answerText(item.selected_answer, item.options)}</p><p>正确答案：{answerText(item.correct_answer, item.options)}</p>
          {item.explanation && <p>解析：{item.explanation}</p>}
          <QuestionImageViewer parts={item.image_parts} imagePreferred={isImagePreferredMode(item.display_mode)} />
        </article>
      ))}</div>
      <div className="practice-result-actions">
        <button type="button" onClick={() => { localStorage.removeItem(LAST_PRACTICE_SESSION_KEY); setScreen("settings"); setSession(null); setResult(null); }}>返回刷题设置</button>
        {onOpenWrongBook && <button type="button" onClick={onOpenWrongBook}>进入错题本</button>}
      </div>
    </section>
  );

  if (!session || !current) return null;
  return (
    <section className="practice-workspace" aria-labelledby="practice-question-title">
      <div className="practice-heading"><div><p className="eyebrow">QUESTION {currentIndex + 1} / {session.actual_count}</p><h2 id="practice-question-title">{current.stem}</h2></div><span>{current.question_type === "single_choice" ? "单选题" : "判断题"} · {current.course_name} · {current.category_path} · 批次位置 {current.batch_position}</span></div>
      {session.notice && <p className="practice-notice">{session.notice}</p>}
      {error && <p role="alert" className="form-error">{error}</p>}
      <QuestionImageViewer parts={current.image_parts} imagePreferred={isImagePreferredMode(current.display_mode)} />
      {current.question_type === "single_choice" ? (
        <fieldset className="practice-options"><legend>请选择一个答案</legend>{current.options.map((option) => <label key={option.label} className={currentAnswer === option.label ? "selected" : ""}>
          <input type="radio" name={`answer-${current.id}`} value={option.label} checked={currentAnswer === option.label} disabled={busy || current.locked || session.status === "submitted"} onChange={() => chooseAnswer(option.label)} />
          <strong>{option.label}</strong><span>{option.text}</span>
        </label>)}</fieldset>
      ) : (
        <div className="practice-boolean" aria-label="判断题答案">
          <button type="button" disabled={busy || current.locked || session.status === "submitted"} className={currentAnswer === true ? "selected" : ""} onClick={() => chooseAnswer(true)}>✓ 正确</button>
          <button type="button" disabled={busy || current.locked || session.status === "submitted"} className={currentAnswer === false ? "selected" : ""} onClick={() => chooseAnswer(false)}>✗ 错误</button>
        </div>
      )}
      {session.feedback_mode === "immediate" && !current.locked && <button type="button" className="primary-action" disabled={busy || currentAnswer === null || currentAnswer === undefined} onClick={() => void answer(currentAnswer as string | boolean)}>提交本题</button>}
      {current.is_correct !== undefined && current.is_correct !== null && <section className={current.is_correct ? "practice-feedback correct" : "practice-feedback incorrect"}><strong>{current.is_correct ? "回答正确" : "回答错误"}</strong><p>正确答案：{answerText(current.correct_answer, current.options)}</p>{current.explanation && <p>{current.explanation}</p>}</section>}
      <nav className="practice-question-nav" aria-label="题目导航">
        <button type="button" disabled={currentIndex === 0} onClick={() => setCurrentIndex((value) => value - 1)}>上一题</button>
        <div>{session.items.map((item, index) => <button type="button" aria-current={index === currentIndex ? "page" : undefined} className={item.locked ? "locked" : item.saved ? "saved" : ""} onClick={() => setCurrentIndex(index)} key={item.id}>{index + 1}<small>{item.locked ? "已锁定" : item.saved ? "已保存" : "未答"}</small></button>)}</div>
        <button type="button" disabled={currentIndex === session.items.length - 1} onClick={() => setCurrentIndex((value) => value + 1)}>下一题</button>
      </nav>
      <button type="button" className="primary-action" disabled={busy} onClick={() => void submit()}>提交整套答案</button>
    </section>
  );
}
