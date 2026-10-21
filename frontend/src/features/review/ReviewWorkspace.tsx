import { useEffect, useMemo, useRef, useState } from "react";

import { OrderingReview } from "../ordering/OrderingReview";
import { deleteReviewItem, getReviewBatchContext, getReviewItem, getReviewItems, mergeReviewItem, postReviewAction, saveReviewItem, saveReviewRelationshipCrops, type ReviewBatchContext, type ReviewBundle, type ReviewItem, type ReviewRelationship } from "./api";


const anomalyLabels: Record<string, string> = {
  MISSING_TITLE: "未识别题目标题",
  MISSING_ANSWER: "未找到正确答案",
  CONFLICTING_ANSWERS: "答案冲突",
  NO_OPTIONS: "没有识别到选项",
  TOO_FEW_OPTIONS: "选项少于 2 个",
  NONCONTIGUOUS_OPTIONS: "选项字母不连续",
  ANSWER_OUT_OF_RANGE: "答案不在选项中",
  TRUE_FALSE_MAPPING_FAILED: "判断题映射失败",
  QUESTION_TYPE_ANSWER_CONFLICT: "题型与答案冲突",
  LOW_OCR_CONFIDENCE: "OCR 置信度低",
  FRAGMENTARY_OCR: "OCR 文本破碎",
  EMPTY_OCR: "OCR 结果为空",
  TRUNCATED_TOP: "顶部疑似截断",
  TRUNCATED_BOTTOM: "底部疑似截断",
  UNCONFIRMED_CROSS_SCREEN: "跨截图拼接未确认",
  UNCERTAIN_ORDER: "截图顺序不确定",
  SUSPECTED_DUPLICATE: "疑似重复题",
  ANSWER_IN_CROP: "裁切区域包含答案",
  UNSUPPORTED_QUESTION_TYPE: "不支持的题型",
  CORRUPT_IMAGE: "图片损坏",
};

const anomalyOptions = Object.entries(anomalyLabels);

function cloneItem(item: ReviewItem): ReviewItem {
  return JSON.parse(JSON.stringify(item)) as ReviewItem;
}

function normalizeQuestionType(current: ReviewItem, questionType: ReviewItem["question_type"]): ReviewItem {
  const labels = current.options.map((option) => option.label);
  if (questionType === "true_false") {
    const trueLabel = current.true_label && labels.includes(current.true_label)
      ? current.true_label
      : labels[0] ?? null;
    const falseLabel = current.false_label && labels.includes(current.false_label) && current.false_label !== trueLabel
      ? current.false_label
      : labels.find((label) => label !== trueLabel) ?? null;
    const correctAnswer = typeof current.correct_answer === "boolean"
      ? current.correct_answer
      : current.correct_answer !== falseLabel;
    return {
      ...current,
      question_type: questionType,
      correct_answer: correctAnswer,
      true_label: trueLabel,
      false_label: falseLabel,
    };
  }

  let correctAnswer: string | null;
  if (typeof current.correct_answer === "string" && labels.includes(current.correct_answer)) {
    correctAnswer = current.correct_answer;
  } else {
    correctAnswer = current.correct_answer === false ? current.false_label : current.true_label;
  }
  return {
    ...current,
    question_type: questionType,
    correct_answer: correctAnswer && labels.includes(correctAnswer) ? correctAnswer : labels[0] ?? null,
  };
}

function formatAnswer(answer: ReviewItem["correct_answer"]): string {
  if (answer === true) return "正确";
  if (answer === false) return "错误";
  return answer || "未识别";
}

function isDuplicateRelationship(relationship: ReviewRelationship): boolean {
  return relationship.relation_type === "same_batch_duplicate" || relationship.relation_type === "manual_duplicate";
}

function relationshipName(relationship: ReviewRelationship): string {
  return isDuplicateRelationship(relationship) ? "疑似重复题" : "跨截图建议";
}

function relationshipPreview(relationship: ReviewRelationship): string {
  if (relationship.evidence.merged_text_preview) return String(relationship.evidence.merged_text_preview);
  return isDuplicateRelationship(relationship)
    ? "请核对题干、图片和答案是否为同一道题。"
    : "请核对两个截图片段。";
}

type RetentionDecision = "only_primary" | "only_secondary";

type RetentionChoice = {
  decision: RetentionDecision;
  label: string;
  confirmation: string;
};

function sourceBasename(name?: string): string {
  return name?.split(/[\\/]/).filter(Boolean).at(-1)?.trim() ?? "";
}

function screenshotLabel(side: "A" | "B", name?: string): string {
  const basename = sourceBasename(name);
  return basename ? `截图 ${side}（${basename}）` : `截图 ${side}`;
}

function retentionChoices(primaryName?: string, secondaryName?: string): RetentionChoice[] {
  const primary = screenshotLabel("A", primaryName);
  const secondary = screenshotLabel("B", secondaryName);
  return [
    { decision: "only_primary", label: `保留${primary}，忽略${secondary}`, confirmation: `确认保留${primary}并忽略${secondary}？` },
    { decision: "only_secondary", label: `保留${secondary}，忽略${primary}`, confirmation: `确认保留${secondary}并忽略${primary}？` },
  ];
}

function RelationshipRetentionActions({
  primaryName,
  secondaryName,
  disabled = false,
  onResolve,
}: {
  primaryName?: string;
  secondaryName?: string;
  disabled?: boolean;
  onResolve: (decision: RetentionDecision, confirmation: string) => void;
}) {
  return <>{retentionChoices(primaryName, secondaryName).map((choice) => <button
    key={choice.decision}
    type="button"
    disabled={disabled}
    onClick={() => onResolve(choice.decision, choice.confirmation)}
  >{choice.label}</button>)}</>;
}

function clampPartIndex(index: number, partCount: number): number {
  return partCount === 0 ? 0 : Math.min(index, partCount - 1);
}

function RelationshipCandidatePanel({
  label,
  item,
  selectedPartIndex,
  onPartSelect,
  onPartChange,
}: {
  label: "A" | "B";
  item: ReviewItem;
  selectedPartIndex: number;
  onPartSelect: (index: number) => void;
  onPartChange: (key: "source_y_start" | "source_y_end", value: number) => void;
}) {
  const safePartIndex = Math.min(
    selectedPartIndex,
    Math.max(0, item.parts.length - 1),
  );
  const part = item.parts[safePartIndex];
  return <article>
    <h6>截图 {label} · {item.source_image_name}</h6>
    <p>{item.stem}</p>
    <p className="relationship-answer">识别答案：{formatAnswer(item.correct_answer)}</p>
    {item.parts.length > 1 && <div className="part-tabs relationship-part-tabs">
      {item.parts.map((candidatePart, index) => <button
        type="button"
        key={candidatePart.id}
        aria-pressed={index === safePartIndex}
        onClick={() => onPartSelect(index)}
      >截图 {label} 片段 {index + 1}</button>)}
    </div>}
    {part && <>
      <p className="relationship-crop-range">建议裁切范围：{part.source_y_start}–{part.source_y_end}</p>
      <img src={part.source_url} alt={`截图 ${label}：${item.source_image_name}`} />
      <div className="crop-controls relationship-crop-controls">
        <label>上边界 <output>{part.source_y_start}</output><input aria-label={`截图 ${label} 裁切上边界`} type="range" min="0" max={part.source_y_end - 1} value={part.source_y_start} onChange={(event) => onPartChange("source_y_start", Number(event.target.value))} /></label>
        <label>下边界 <output>{part.source_y_end}</output><input aria-label={`截图 ${label} 裁切下边界`} type="range" min={part.source_y_start + 1} max={part.source_height} value={part.source_y_end} onChange={(event) => onPartChange("source_y_end", Number(event.target.value))} /></label>
      </div>
      <img className="crop-preview" src={part.crop_url} alt={`截图 ${label} 建议裁切`} />
    </>}
  </article>;
}

export function ReviewWorkspace({ jobId, onChanged }: { jobId?: string; onChanged?: () => void }) {
  const [bundle, setBundle] = useState<ReviewBundle | null>(null);
  const [activeId, setActiveId] = useState("");
  const [draft, setDraft] = useState<ReviewItem | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [questionType, setQuestionType] = useState("");
  const [anomalyCode, setAnomalyCode] = useState("");
  const [severity, setSeverity] = useState("");
  const [handlingStatus, setHandlingStatus] = useState("pending");
  const [courseFilter, setCourseFilter] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("");
  const [batchFilter, setBatchFilter] = useState("");
  const [zoom, setZoom] = useState(100);
  const [partIndex, setPartIndex] = useState(0);
  const [splitY, setSplitY] = useState("");
  const [splitStem, setSplitStem] = useState("");
  const [duplicateOf, setDuplicateOf] = useState("");
  const [mergeWith, setMergeWith] = useState("");
  const [batchContext, setBatchContext] = useState<ReviewBatchContext | null>(null);
  const [batchContextLoading, setBatchContextLoading] = useState(false);
  const batchContextRequest = useRef(0);
  const [activeRelationshipId, setActiveRelationshipId] = useState("");
  const [relationshipPair, setRelationshipPair] = useState<{ primary: ReviewItem; secondary: ReviewItem } | null>(null);
  const [primaryRelationshipPartIndex, setPrimaryRelationshipPartIndex] = useState(0);
  const [secondaryRelationshipPartIndex, setSecondaryRelationshipPartIndex] = useState(0);
  const [orderingBatchId, setOrderingBatchId] = useState("");

  function clearBatchContext() {
    batchContextRequest.current += 1;
    setBatchContext(null);
    setBatchContextLoading(false);
    setDuplicateOf("");
    setMergeWith("");
  }

  async function loadBatchContext(candidateId: string) {
    const requestId = ++batchContextRequest.current;
    setError("");
    setBatchContext(null);
    setBatchContextLoading(true);
    setDuplicateOf("");
    setMergeWith("");
    try {
      const next = await getReviewBatchContext(candidateId);
      if (requestId !== batchContextRequest.current) return;
      const targets = next.items.filter((item) => item.id !== candidateId);
      const firstTargetId = targets[0]?.id ?? "";
      setBatchContext(next);
      setDuplicateOf(firstTargetId);
      setMergeWith(firstTargetId);
    } catch (reason) {
      if (requestId !== batchContextRequest.current) return;
      setBatchContext(null);
      setError(reason instanceof Error ? reason.message : "读取批次候选失败");
    } finally {
      if (requestId === batchContextRequest.current) setBatchContextLoading(false);
    }
  }

  async function load(preferredId?: string) {
    try {
      const next = await getReviewItems(jobId, {
        course_name: jobId ? "" : courseFilter,
        category_path: jobId ? "" : categoryFilter,
        batch_name: jobId ? "" : batchFilter,
        anomaly_code: anomalyCode,
        question_type: questionType,
        severity,
        handling_status: handlingStatus,
      });
      setBundle(next);
      const selected = next.items.find((item) => item.id === (preferredId ?? activeId)) ?? next.items[0] ?? null;
      setActiveId(selected?.id ?? "");
      setDraft(selected ? cloneItem(selected) : null);
      setPartIndex(0);
      if (selected) {
        void loadBatchContext(selected.id);
        setActiveRelationshipId("");
        setPrimaryRelationshipPartIndex(0);
        setSecondaryRelationshipPartIndex(0);
        setRelationshipPair(null);
      } else {
        clearBatchContext();
        const selectedRelationship = next.relationships.find((item) => item.id === activeRelationshipId && item.status === "pending")
          ?? next.relationships.find((item) => item.status === "pending")
          ?? null;
        setActiveRelationshipId(selectedRelationship?.id ?? "");
        if (selectedRelationship) {
          setPrimaryRelationshipPartIndex(0);
          setSecondaryRelationshipPartIndex(0);
          setRelationshipPair(null);
          const [primary, secondary] = await Promise.all([
            getReviewItem(selectedRelationship.primary_candidate_id),
            getReviewItem(selectedRelationship.secondary_candidate_id),
          ]);
          setPrimaryRelationshipPartIndex(0);
          setSecondaryRelationshipPartIndex(0);
          setRelationshipPair({ primary, secondary });
        } else {
          setPrimaryRelationshipPartIndex(0);
          setSecondaryRelationshipPartIndex(0);
          setRelationshipPair(null);
        }
      }
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "读取待检查题失败");
    }
  }

  useEffect(() => {
    void load();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, courseFilter, categoryFilter, batchFilter, anomalyCode, questionType, severity, handlingStatus]);

  function selectItem(id: string) {
    const item = bundle?.items.find((candidate) => candidate.id === id);
    if (!item) return;
    setActiveId(id);
    setDraft(cloneItem(item));
    setPartIndex(0);
    void loadBatchContext(id);
    setActiveRelationshipId("");
    setPrimaryRelationshipPartIndex(0);
    setSecondaryRelationshipPartIndex(0);
    setRelationshipPair(null);
  }

  async function selectRelationship(relationship: ReviewRelationship) {
    setActiveRelationshipId(relationship.id);
    setPrimaryRelationshipPartIndex(0);
    setSecondaryRelationshipPartIndex(0);
    setRelationshipPair(null);
    setDraft(null);
    clearBatchContext();
    setBusy(true);
    try {
      const [primary, secondary] = await Promise.all([
        getReviewItem(relationship.primary_candidate_id),
        getReviewItem(relationship.secondary_candidate_id),
      ]);
      setPrimaryRelationshipPartIndex(0);
      setSecondaryRelationshipPartIndex(0);
      setRelationshipPair({ primary, secondary });
      setError("");
    } catch (reason) {
      setRelationshipPair(null);
      setError(reason instanceof Error ? reason.message : "读取跨截图关系失败");
    } finally {
      setBusy(false);
    }
  }

  function setField<K extends keyof ReviewItem>(key: K, value: ReviewItem[K]) {
    setDraft((current) => current ? { ...current, [key]: value } : current);
  }

  function changeQuestionType(questionType: ReviewItem["question_type"]) {
    setDraft((current) => current ? normalizeQuestionType(current, questionType) : current);
  }

  function updateOption(index: number, text: string) {
    setDraft((current) => current ? {
      ...current,
      options: current.options.map((option, optionIndex) => optionIndex === index ? { ...option, text } : option),
    } : current);
  }

  function updateOptionLabel(index: number, label: string) {
    setDraft((current) => {
      if (!current) return current;
      const oldLabel = current.options[index]?.label;
      const nextLabel = label.toUpperCase().slice(0, 1);
      const changed = {
        ...current,
        options: current.options.map((option, optionIndex) => optionIndex === index ? { ...option, label: nextLabel } : option),
        correct_answer: typeof current.correct_answer === "string" && current.correct_answer === oldLabel ? nextLabel : current.correct_answer,
        true_label: current.true_label === oldLabel ? nextLabel : current.true_label,
        false_label: current.false_label === oldLabel ? nextLabel : current.false_label,
      };
      return normalizeQuestionType(changed, changed.question_type);
    });
  }

  function addOption() {
    setDraft((current) => {
      if (!current || current.options.length >= 8) return current;
      const label = String.fromCharCode(65 + current.options.length);
      return { ...current, options: [...current.options, { label, text: "", order_index: current.options.length }] };
    });
  }

  function removeOption(index: number) {
    setDraft((current) => {
      if (!current || current.options.length <= 2) return current;
      const remaining = current.options.filter((_, optionIndex) => optionIndex !== index);
      const labelMap = new Map(remaining.map((option, optionIndex) => [option.label, String.fromCharCode(65 + optionIndex)]));
      const options = remaining.map((option, optionIndex) => ({ ...option, label: String.fromCharCode(65 + optionIndex), order_index: optionIndex }));
      const changed = {
        ...current,
        options,
        correct_answer: typeof current.correct_answer === "string" ? labelMap.get(current.correct_answer) ?? null : current.correct_answer,
        true_label: current.true_label ? labelMap.get(current.true_label) ?? null : null,
        false_label: current.false_label ? labelMap.get(current.false_label) ?? null : null,
      };
      return normalizeQuestionType(changed, changed.question_type);
    });
  }

  function updatePart(key: "source_y_start" | "source_y_end", value: number) {
    setDraft((current) => current ? {
      ...current,
      parts: current.parts.map((part, index) => index === partIndex ? { ...part, [key]: value } : part),
    } : current);
  }

  function updateRelationshipPart(
    side: "primary" | "secondary",
    targetPartIndex: number,
    key: "source_y_start" | "source_y_end",
    value: number,
  ) {
    setRelationshipPair((current) => current ? {
      ...current,
      [side]: {
        ...current[side],
        parts: current[side].parts.map((part, index) => index === targetPartIndex ? { ...part, [key]: value } : part),
      },
    } : current);
  }

  async function save() {
    if (!draft || busy) return;
    setBusy(true);
    try {
      const saved = await saveReviewItem(draft);
      setDraft(cloneItem(saved));
      setError("");
      onChanged?.();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败");
    } finally {
      setBusy(false);
    }
  }

  async function saveRelationshipCrops() {
    if (!activeRelationship || !relationshipPair || busy) return;
    setBusy(true);
    try {
      const { primary, secondary } = await saveReviewRelationshipCrops(
        activeRelationship.id,
        relationshipPair.primary,
        relationshipPair.secondary,
      );
      setPrimaryRelationshipPartIndex((current) => (
        clampPartIndex(current, primary.parts.length)
      ));
      setSecondaryRelationshipPartIndex((current) => (
        clampPartIndex(current, secondary.parts.length)
      ));
      setRelationshipPair({ primary: cloneItem(primary), secondary: cloneItem(secondary) });
      setError("");
      onChanged?.();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存两侧裁切范围失败");
    } finally {
      setBusy(false);
    }
  }

  async function action(path: string, body?: Record<string, unknown>, message?: string) {
    if (message && !window.confirm(message)) return;
    setBusy(true);
    try {
      await postReviewAction(path, body);
      await load(activeId);
      onChanged?.();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }

  async function mutation(operation: () => Promise<unknown>, message: string) {
    if (!window.confirm(message)) return;
    setBusy(true);
    try {
      await operation();
      await load(activeId);
      onChanged?.();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }

  const currentPart = draft?.parts[Math.min(partIndex, Math.max(0, draft.parts.length - 1))] ?? null;
  const relation = draft?.relationships.find((item) => item.status === "pending") ?? null;
  const activeRelationship = bundle?.relationships.find((item) => item.id === activeRelationshipId && item.status === "pending") ?? null;
  const inlineRelationshipPrimary = relation
    ? bundle?.items.find((item) => item.id === relation.primary_candidate_id)
    : undefined;
  const inlineRelationshipSecondary = relation
    ? bundle?.items.find((item) => item.id === relation.secondary_candidate_id)
    : undefined;
  const relationIsDuplicate = relation ? isDuplicateRelationship(relation) : false;
  const activeRelationshipIsDuplicate = activeRelationship ? isDuplicateRelationship(activeRelationship) : false;
  const confidence = useMemo(() => relation ? `${Math.round(relation.confidence * 100)}%` : "", [relation]);
  const batchItems = batchContext?.items ?? [];
  const batchIndex = draft ? batchItems.findIndex((item) => item.id === draft.id) : -1;
  const previousItem = batchIndex > 0 ? batchItems[batchIndex - 1] : null;
  const nextItem = batchIndex >= 0 && batchIndex < batchItems.length - 1 ? batchItems[batchIndex + 1] : null;
  const batchTargets = draft ? batchItems.filter((item) => item.id !== draft.id) : [];
  const batchContextUnavailable = batchContextLoading || batchContext === null || batchIndex < 0;
  const courses = useMemo(() => [...new Set([
    ...(bundle?.items.map((item) => item.course_name) ?? []),
    ...(bundle?.relationships.map((item) => item.course_name) ?? []),
    ...(bundle?.ordering_issues.map((item) => item.course_name) ?? []),
  ])], [bundle]);
  const categories = useMemo(() => [...new Set([
    ...(bundle?.items.map((item) => item.category_path) ?? []),
    ...(bundle?.relationships.map((item) => item.category_path) ?? []),
    ...(bundle?.ordering_issues.map((item) => item.category_path) ?? []),
  ])], [bundle]);
  const batches = useMemo(() => [...new Set([
    ...(bundle?.items.map((item) => item.batch_name) ?? []),
    ...(bundle?.relationships.map((item) => item.batch_name) ?? []),
    ...(bundle?.ordering_issues.map((item) => item.batch_name) ?? []),
  ])], [bundle]);
  const activeOrderingIssue = bundle?.ordering_issues.find((item) => item.batch_id === (orderingBatchId || jobId))
    ?? bundle?.ordering_issues[0]
    ?? null;
  const orderingActionIssueIds = useMemo(() => {
    const firstPendingByBatch = new Map<string, string>();
    for (const issue of bundle?.ordering_issues ?? []) {
      if (issue.status === "pending" && !firstPendingByBatch.has(issue.batch_id)) {
        firstPendingByBatch.set(issue.batch_id, issue.id);
      }
    }
    return new Set(firstPendingByBatch.values());
  }, [bundle]);
  const fixedCourse = jobId ? draft?.course_name ?? activeRelationship?.course_name ?? activeOrderingIssue?.course_name ?? "" : courseFilter;
  const fixedCategory = jobId ? draft?.category_path ?? activeRelationship?.category_path ?? activeOrderingIssue?.category_path ?? "" : categoryFilter;
  const fixedBatch = jobId ? draft?.batch_name ?? activeRelationship?.batch_name ?? activeOrderingIssue?.batch_name ?? "" : batchFilter;

  if (!bundle && !error) return <p role="status">正在读取异常题…</p>;
  if (!bundle) return <p className="import-warning" role="alert">{error}</p>;

  return (
    <section className="review-workspace" aria-labelledby={`review-title-${jobId ?? "all"}`}>
      <div className="review-heading">
        <div><span className="progress-kicker">QUESTION REVIEW</span><h4 id={`review-title-${jobId ?? "all"}`}>异常题人工检查</h4></div>
        <strong>{bundle.items.length + bundle.relationships.length + bundle.ordering_issues.length} 项待核对</strong>
      </div>

      <div className="review-filters" aria-label="待检查筛选">
        <label>异常类型<select aria-label="异常类型筛选" value={anomalyCode} onChange={(event) => setAnomalyCode(event.target.value)}><option value="">全部</option>{anomalyOptions.map(([code, label]) => <option key={code} value={code}>{label}</option>)}</select></label>
        <label>课程<select aria-label="课程筛选" value={fixedCourse} disabled={Boolean(jobId)} onChange={(event) => setCourseFilter(event.target.value)}><option value="">全部</option>{(jobId && fixedCourse ? [fixedCourse] : courses).map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
        <label>分类<select aria-label="分类筛选" value={fixedCategory} disabled={Boolean(jobId)} onChange={(event) => setCategoryFilter(event.target.value)}><option value="">全部</option>{(jobId && fixedCategory ? [fixedCategory] : categories).map((value) => <option key={value} value={value}>{value || "未分类"}</option>)}</select></label>
        <label>批次<select aria-label="批次筛选" value={fixedBatch} disabled={Boolean(jobId)} onChange={(event) => setBatchFilter(event.target.value)}><option value="">全部</option>{(jobId && fixedBatch ? [fixedBatch] : batches).map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
        <label>题型<select aria-label="题型筛选" value={questionType} onChange={(event) => setQuestionType(event.target.value)}><option value="">全部</option><option value="single_choice">单选题</option><option value="true_false">判断题</option></select></label>
        <label>严重程度<select aria-label="严重程度筛选" value={severity} onChange={(event) => setSeverity(event.target.value)}><option value="">全部</option><option value="error">错误</option><option value="warning">警告</option></select></label>
        <label>处理状态<select aria-label="处理状态筛选" value={handlingStatus} onChange={(event) => setHandlingStatus(event.target.value)}><option value="pending">未处理</option><option value="processed">已处理</option><option value="all">全部</option></select></label>
      </div>

      {bundle.ordering_issues.length > 0 && <ul className="review-order-issues">{bundle.ordering_issues.map((issue) => <li key={issue.id}><span>{issue.message}</span>{orderingActionIssueIds.has(issue.id) && <button type="button" onClick={() => setOrderingBatchId(issue.batch_id)}>检查该批次截图顺序</button>}</li>)}</ul>}

      {orderingBatchId && <OrderingReview
        jobId={orderingBatchId}
        onConfirmed={() => {
          setOrderingBatchId("");
          void load();
          onChanged?.();
        }}
      />}

      {bundle.items.length === 0 && bundle.relationships.length === 0 && bundle.ordering_issues.length === 0 ? <p className="review-empty">当前筛选条件下没有待处理内容。</p> : (
        <div className="review-layout">
          <div className="review-sidebar">
          {bundle.items.length > 0 && <aside className="review-queue" aria-label="待检查题目列表">
            {bundle.items.map((item) => <button type="button" key={item.id} className={item.id === activeId ? "active" : ""} onClick={() => selectItem(item.id)}><span>{item.question_type === "true_false" ? "判断" : "单选"}</span><strong>{item.stem}</strong><small>{item.anomaly_flags[0]?.message ?? item.status}</small></button>)}
          </aside>}

          {bundle.relationships.length > 0 && <aside className="review-queue relationship-queue" aria-label="待处理候选关系列表">
            {bundle.relationships.map((item) => <button type="button" key={item.id} className={item.id === activeRelationshipId ? "active" : ""} onClick={() => void selectRelationship(item)}><span>{relationshipName(item)} · {Math.round(item.confidence * 100)}%</span><strong>{relationshipPreview(item)}</strong><small>{item.status === "pending" ? "等待人工决定" : item.decision}</small></button>)}
          </aside>}
          </div>

          {draft && <div className="review-editor">
            <section className="review-visual" aria-label="原图与裁切框">
              <div className="visual-toolbar">
                <label>缩放<input aria-label="原图缩放" type="range" min="50" max="200" value={zoom} onChange={(event) => setZoom(Number(event.target.value))} /></label><span>{zoom}%</span>
              </div>
              {draft.parts.length > 1 && <div className="part-tabs">{draft.parts.map((part, index) => <button type="button" key={part.id} onClick={() => setPartIndex(index)}>片段 {index + 1}</button>)}</div>}
              {currentPart && <div className="source-stage"><div className="source-canvas" style={{ width: `${zoom}%` }}><img src={currentPart.source_url} alt={`原始截图 ${draft.source_image_name}`} /><div className="crop-mask crop-mask-top" style={{ height: `${currentPart.source_y_start / currentPart.source_height * 100}%` }} /><div className="crop-frame" style={{ top: `${currentPart.source_y_start / currentPart.source_height * 100}%`, height: `${(currentPart.source_y_end - currentPart.source_y_start) / currentPart.source_height * 100}%` }} /></div></div>}
              {currentPart && <div className="crop-controls"><label>上边界 <output>{currentPart.source_y_start}</output><input aria-label="裁切上边界" type="range" min="0" max={currentPart.source_y_end - 1} value={currentPart.source_y_start} onChange={(event) => updatePart("source_y_start", Number(event.target.value))} /></label><label>下边界 <output>{currentPart.source_y_end}</output><input aria-label="裁切下边界" type="range" min={currentPart.source_y_start + 1} max={currentPart.source_height} value={currentPart.source_y_end} onChange={(event) => updatePart("source_y_end", Number(event.target.value))} /></label></div>}
              <img className="crop-preview" src={currentPart?.crop_url} alt="当前裁切预览" />
            </section>

            <section className="review-fields" aria-label="题目字段编辑">
              <div className="confidence-row"><span>OCR {Math.round(draft.ocr_confidence * 100)}%</span><span>解析 {Math.round(draft.parse_confidence * 100)}%</span></div>
              <label>原始 OCR 文本<textarea aria-label="原始 OCR 文本" value={draft.raw_ocr_text} readOnly /></label>
              <label>编辑 OCR 文本<textarea aria-label="编辑 OCR 文本" value={draft.edited_ocr_text ?? ""} onChange={(event) => setField("edited_ocr_text", event.target.value)} /></label>
              <label>编辑后的题干<textarea aria-label="编辑后的题干" value={draft.stem} onChange={(event) => setField("stem", event.target.value)} /></label>
              <label>题型<select aria-label="题型" value={draft.question_type} onChange={(event) => changeQuestionType(event.target.value as ReviewItem["question_type"])}><option value="single_choice">单选题</option><option value="true_false">判断题</option></select></label>
              <fieldset className="option-editor"><legend>选项列表</legend>{draft.options.map((option, index) => <div key={`${index}-${option.label}`}><input className="option-label-input" aria-label={`选项 ${option.label} 字母`} maxLength={1} value={option.label} onChange={(event) => updateOptionLabel(index, event.target.value)} /><input aria-label={`选项 ${option.label} 文本`} value={option.text} onChange={(event) => updateOption(index, event.target.value)} /><button type="button" aria-label={`删除选项 ${option.label}`} disabled={draft.options.length <= 2} onClick={() => removeOption(index)}>删除</button></div>)}<button type="button" onClick={addOption} disabled={draft.options.length >= 8}>新增选项</button></fieldset>
              {draft.question_type === "single_choice" ? <label>正确答案<select aria-label="正确答案" value={typeof draft.correct_answer === "string" ? draft.correct_answer : ""} onChange={(event) => setField("correct_answer", event.target.value)}><option value="">请选择</option>{draft.options.map((option) => <option key={option.label} value={option.label}>{option.label}</option>)}</select></label> : <><label>判断答案<select aria-label="判断答案" value={draft.correct_answer === true ? "true" : draft.correct_answer === false ? "false" : ""} onChange={(event) => setField("correct_answer", event.target.value === "" ? null : event.target.value === "true")}><option value="">请选择</option><option value="true">正确</option><option value="false">错误</option></select></label><label>“正确”选项<select aria-label="正确选项映射" value={draft.true_label ?? ""} onChange={(event) => setField("true_label", event.target.value || null)}><option value="">请选择</option>{draft.options.map((option) => <option key={option.label}>{option.label}</option>)}</select></label><label>“错误”选项<select aria-label="错误选项映射" value={draft.false_label ?? ""} onChange={(event) => setField("false_label", event.target.value || null)}><option value="">请选择</option>{draft.options.map((option) => <option key={option.label}>{option.label}</option>)}</select></label></>}
              <label>解析文本<textarea aria-label="解析文本" value={draft.explanation ?? ""} onChange={(event) => setField("explanation", event.target.value)} /></label>
              <label>显示模式<select aria-label="显示模式" value={draft.display_mode} onChange={(event) => setField("display_mode", event.target.value)}><option value="text">文字优先</option><option value="text_first">文字优先（人工）</option><option value="image_preferred">图片优先</option><option value="image_first">图片优先（人工）</option></select></label>
              <ul className="anomaly-list">{draft.anomaly_flags.map((flag) => <li key={flag.code}><strong>{anomalyLabels[flag.code] ?? flag.code}</strong><span>{flag.message}</span></li>)}</ul>
              {error && <p className="import-warning" role="alert">{error}</p>}
              <div className="primary-review-actions"><button type="button" disabled={busy} onClick={save}>保存修改</button><button type="button" disabled={busy} onClick={() => action(`/api/review-items/${draft.id}/confirm`)}>确认并入库</button>{draft.can_undo && <button type="button" disabled={busy} onClick={() => action(`/api/review-items/${draft.id}/undo`)}>撤销最近一次修改</button>}</div>
              <div className="secondary-review-actions"><button type="button" disabled={busy} onClick={() => action(`/api/review-items/${draft.id}/ignore`, { confirmed: true }, "确认忽略这个候选题？")}>忽略候选</button><button type="button" disabled={busy} onClick={() => mutation(() => deleteReviewItem(draft.id), "确认删除这个候选题？原始截图仍会保留。")}>删除候选</button><button type="button" disabled={busy} onClick={() => action(`/api/review-items/${draft.id}/reorder`, { direction: "up", confirmed: true }, "确认将来源截图上移？")}>截图上移</button><button type="button" disabled={busy} onClick={() => action(`/api/review-items/${draft.id}/reorder`, { direction: "down", confirmed: true }, "确认将来源截图下移？")}>截图下移</button><button type="button" disabled={busy} onClick={() => action(`/api/review-items/${draft.id}/rerun-ocr`, { confirmed: true }, "确认重新运行单题 OCR？当前编辑字段将被新结果替换。")}>重新运行单题 OCR</button><button type="button" disabled={busy} onClick={() => action(`/api/review-items/${draft.id}/rerun-source-ocr`, { confirmed: true }, "确认重新运行整张图 OCR？")}>重新运行整张图 OCR</button></div>
              <div className="split-controls"><label>拆分线<input aria-label="拆分裁切线" type="number" value={splitY} onChange={(event) => setSplitY(event.target.value)} /></label><label>第二题题干<input aria-label="拆分后第二题题干" value={splitStem} onChange={(event) => setSplitStem(event.target.value)} /></label><button type="button" disabled={!splitY || !splitStem || busy} onClick={() => action(`/api/review-items/${draft.id}/split`, { split_y: Number(splitY), second_stem: splitStem, confirmed: true }, "确认把当前候选拆分成两题？")}>拆分成两题</button></div>
              <div className="duplicate-controls"><label>重复于<select aria-label="重复题目标" value={duplicateOf} onChange={(event) => setDuplicateOf(event.target.value)}>{batchTargets.map((item) => <option key={item.id} value={item.id}>{item.stem}</option>)}</select></label><button type="button" disabled={!duplicateOf || busy || batchContextUnavailable} onClick={() => action(`/api/review-items/${draft.id}/duplicate`, { duplicate_of_id: duplicateOf, confirmed: true }, "确认将此题标记为同批重复？")}>标记为重复</button></div>
              <div className="merge-controls"><label>合并目标<select aria-label="合并题目目标" value={mergeWith} onChange={(event) => setMergeWith(event.target.value)}>{batchTargets.map((item) => <option key={item.id} value={item.id}>{item.stem}</option>)}</select></label><button type="button" disabled={!mergeWith || busy || batchContextUnavailable} onClick={() => mutation(() => mergeReviewItem(draft.id, mergeWith), "确认合并所选题目？两个候选和原始截图都将保留在审计记录中。")}>合并所选题目</button><button type="button" disabled={!previousItem || busy || batchContextUnavailable} onClick={() => previousItem && mutation(() => mergeReviewItem(previousItem.id, draft.id), "确认把当前题合并到上一题？两个候选和原始截图都将保留在审计记录中。")}>合并上一题</button><button type="button" disabled={!nextItem || busy || batchContextUnavailable} onClick={() => nextItem && mutation(() => mergeReviewItem(draft.id, nextItem.id), "确认把下一题合并到当前题？两个候选和原始截图都将保留在审计记录中。")}>合并下一题</button></div>
              {relation && <section className="relationship-card" aria-label={relationshipName(relation)}><strong>{relationshipName(relation)} · {confidence}</strong><p>{relationshipPreview(relation)}</p>{relationIsDuplicate ? <div><button type="button" onClick={() => action(`/api/review-items/${relation.secondary_candidate_id}/duplicate`, { duplicate_of_id: relation.primary_candidate_id, confirmed: true }, "确认将后一题标记为前一题的重复题？")}>确认标记关系为重复题</button><button type="button" onClick={() => action(`/api/review-relationships/${relation.id}/resolve`, { decision: "separate", confirmed: true }, "确认保留为两道不同题？")}>保留为两道不同题</button></div> : <div><button type="button" onClick={() => mutation(() => mergeReviewItem(relation.primary_candidate_id, relation.secondary_candidate_id, relation.id), "确认合并这两个截图片段？")}>确认跨截图合并</button><button type="button" onClick={() => action(`/api/review-relationships/${relation.id}/resolve`, { decision: "swap", confirmed: true }, "确认调换两个片段顺序？")}>调换片段顺序</button><RelationshipRetentionActions primaryName={inlineRelationshipPrimary?.source_image_name} secondaryName={inlineRelationshipSecondary?.source_image_name} onResolve={(decision, confirmation) => action(`/api/review-relationships/${relation.id}/resolve`, { decision, confirmed: true }, confirmation)} /><button type="button" onClick={() => action(`/api/review-relationships/${relation.id}/resolve`, { decision: "separate", confirmed: true }, "确认保持为两道独立题？")}>保持两题分离</button><button type="button" onClick={() => action(`/api/review-relationships/${relation.id}/resolve`, { decision: "two_questions", confirmed: true }, "确认标记为两道不同题？")}>标记为两道不同题</button><button type="button" onClick={() => action(`/api/review-relationships/${relation.id}/resolve`, { decision: "delete", confirmed: true }, "确认删除后一候选？")}>删除合并候选</button></div>}</section>}
            </section>
          </div>}

          {!draft && activeRelationship && relationshipPair && <section className="relationship-detail" aria-label={activeRelationshipIsDuplicate ? "疑似重复题详情" : "跨截图建议详情"}>
            <div className="relationship-detail-heading"><div><span className="progress-kicker">RELATIONSHIP REVIEW</span><h5>{activeRelationshipIsDuplicate ? "疑似重复题 A / B 对照" : "截图 A / B 对照"}</h5></div><strong>{Math.round(activeRelationship.confidence * 100)}%</strong></div>
            <p className="relationship-preview">{relationshipPreview(activeRelationship)}</p>
            <div className="relationship-compare">
              <RelationshipCandidatePanel
                label="A"
                item={relationshipPair.primary}
                selectedPartIndex={primaryRelationshipPartIndex}
                onPartSelect={setPrimaryRelationshipPartIndex}
                onPartChange={(key, value) => updateRelationshipPart(
                  "primary",
                  primaryRelationshipPartIndex,
                  key,
                  value,
                )}
              />
              <RelationshipCandidatePanel
                label="B"
                item={relationshipPair.secondary}
                selectedPartIndex={secondaryRelationshipPartIndex}
                onPartSelect={setSecondaryRelationshipPartIndex}
                onPartChange={(key, value) => updateRelationshipPart(
                  "secondary",
                  secondaryRelationshipPartIndex,
                  key,
                  value,
                )}
              />
            </div>
            {error && <p className="import-warning" role="alert">{error}</p>}
            <button className="relationship-save-crops" type="button" disabled={busy} onClick={() => void saveRelationshipCrops()}>保存两侧裁切范围</button>
            {activeRelationshipIsDuplicate ? <div className="relationship-actions"><button type="button" disabled={busy} onClick={() => action(`/api/review-items/${activeRelationship.secondary_candidate_id}/duplicate`, { duplicate_of_id: activeRelationship.primary_candidate_id, confirmed: true }, "确认将后一题标记为前一题的重复题？")}>确认标记关系为重复题</button><button type="button" disabled={busy} onClick={() => action(`/api/review-relationships/${activeRelationship.id}/resolve`, { decision: "separate", confirmed: true }, "确认保留为两道不同题？")}>保留为两道不同题</button></div> : <div className="relationship-actions"><button type="button" disabled={busy} onClick={() => mutation(() => mergeReviewItem(activeRelationship.primary_candidate_id, activeRelationship.secondary_candidate_id, activeRelationship.id), "确认合并这两个截图片段？")}>确认跨截图合并</button><button type="button" disabled={busy} onClick={() => action(`/api/review-relationships/${activeRelationship.id}/resolve`, { decision: "swap", confirmed: true }, "确认调换片段顺序？")}>调换片段顺序</button><RelationshipRetentionActions primaryName={relationshipPair.primary.source_image_name} secondaryName={relationshipPair.secondary.source_image_name} disabled={busy} onResolve={(decision, confirmation) => action(`/api/review-relationships/${activeRelationship.id}/resolve`, { decision, confirmed: true }, confirmation)} /><button type="button" disabled={busy} onClick={() => action(`/api/review-relationships/${activeRelationship.id}/resolve`, { decision: "separate", confirmed: true }, "确认保持为两道独立题？")}>保持两题分离</button><button type="button" disabled={busy} onClick={() => action(`/api/review-relationships/${activeRelationship.id}/resolve`, { decision: "two_questions", confirmed: true }, "确认标记为两道不同题？")}>标记为两道不同题</button><button type="button" disabled={busy} onClick={() => action(`/api/review-relationships/${activeRelationship.id}/resolve`, { decision: "delete", confirmed: true }, "确认删除后一候选？")}>删除合并候选</button></div>}
          </section>}
        </div>
      )}
    </section>
  );
}
