import { useEffect, useMemo, useRef, useState } from "react";

import {
  deleteBatch,
  deleteQuestion,
  getBatches,
  getCourses,
  getQuestions,
  rerunQuestion,
  saveQuestion,
  type BatchSummary,
  type CourseSummary,
  type LibraryPage,
  type LibraryQuestion,
} from "./api";


const PAGE_SIZE = 50;

type LibraryErrorSource = "metadata" | "questions" | "operation";
type LibraryErrors = Partial<Record<LibraryErrorSource, string>>;


function clone(question: LibraryQuestion): LibraryQuestion {
  return JSON.parse(JSON.stringify(question)) as LibraryQuestion;
}

export function LibraryWorkspace() {
  const [courses, setCourses] = useState<CourseSummary[]>([]);
  const [batches, setBatches] = useState<BatchSummary[]>([]);
  const [page, setPage] = useState<LibraryPage | null>(null);
  const [activeId, setActiveId] = useState("");
  const [draft, setDraft] = useState<LibraryQuestion | null>(null);
  const [currentPage, setCurrentPage] = useState(1);
  const [courseId, setCourseId] = useState("");
  const [categoryId, setCategoryId] = useState("");
  const [batchId, setBatchId] = useState("");
  const [questionType, setQuestionType] = useState("");
  const [search, setSearch] = useState("");
  const [viewMode, setViewMode] = useState<"list" | "thumbnail">("list");
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<LibraryErrors>({});
  const [notice, setNotice] = useState("");
  const metadataRequestId = useRef(0);
  const questionRequestId = useRef(0);

  function clearError(source: LibraryErrorSource) {
    setErrors((current) => {
      if (!current[source]) return current;
      const next = { ...current };
      delete next[source];
      return next;
    });
  }

  function reportError(
    source: LibraryErrorSource,
    reason: unknown,
    fallback: string,
  ) {
    setErrors((current) => ({
      ...current,
      [source]: reason instanceof Error ? reason.message : fallback,
    }));
  }

  async function loadMetadata() {
    const requestId = ++metadataRequestId.current;
    try {
      const [nextCourses, nextBatches] = await Promise.all([
        getCourses(),
        getBatches(courseId, categoryId),
      ]);
      if (requestId !== metadataRequestId.current) return;
      setCourses(nextCourses);
      setBatches(nextBatches);
      clearError("metadata");
    } catch (reason) {
      if (requestId === metadataRequestId.current) {
        reportError("metadata", reason, "读取题库筛选项失败");
      }
    }
  }

  async function loadQuestions(
    preferredId?: string,
    selectedBatchId = batchId,
    requestedPage = currentPage,
  ) {
    const requestId = ++questionRequestId.current;
    try {
      const next = await getQuestions(
        {
          course_id: courseId,
          category_id: categoryId,
          batch_id: selectedBatchId,
          question_type: questionType,
          search,
        },
        requestedPage,
        PAGE_SIZE,
      );
      if (requestId !== questionRequestId.current) return;
      const lastPage = Math.max(1, Math.ceil(next.total / next.page_size));
      if (next.items.length === 0 && requestedPage > lastPage) {
        await loadQuestions(preferredId, selectedBatchId, lastPage);
        return;
      }
      setPage(next);
      setCurrentPage(next.page);
      const selected = next.items.find((item) => item.id === (preferredId ?? activeId)) ?? next.items[0] ?? null;
      setActiveId(selected?.id ?? "");
      setDraft(selected ? clone(selected) : null);
      clearError("questions");
    } catch (reason) {
      if (requestId === questionRequestId.current) {
        reportError("questions", reason, "读取正式题库失败");
      }
    }
  }

  useEffect(() => {
    void loadMetadata();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [courseId, categoryId]);

  useEffect(() => {
    void loadQuestions(undefined, batchId, 1);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [courseId, categoryId, batchId, questionType, search]);

  const filterCategories = useMemo(
    () => (courseId ? courses.find((course) => course.id === courseId)?.categories ?? [] : courses.flatMap((course) => course.categories)),
    [courseId, courses],
  );
  const editCategories = useMemo(
    () => courses.find((course) => course.id === draft?.course_id)?.categories ?? [],
    [courses, draft?.course_id],
  );
  const totalPages = Math.max(
    1,
    Math.ceil((page?.total ?? 0) / (page?.page_size ?? PAGE_SIZE)),
  );

  function selectQuestion(id: string) {
    const selected = page?.items.find((item) => item.id === id);
    if (!selected) return;
    setActiveId(id);
    setDraft(clone(selected));
  }

  function setField<K extends keyof LibraryQuestion>(key: K, value: LibraryQuestion[K]) {
    setDraft((current) => current ? { ...current, [key]: value } : current);
  }

  function changeQuestionType(questionType: LibraryQuestion["question_type"]) {
    setDraft((current) => {
      if (!current || current.question_type === questionType) return current;
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
    });
  }

  function updateOption(index: number, field: "label" | "text", value: string) {
    setDraft((current) => current ? {
      ...current,
      options: current.options.map((option, optionIndex) => optionIndex === index ? { ...option, [field]: field === "label" ? value.toUpperCase().slice(0, 1) : value } : option),
    } : current);
  }

  function addOption() {
    setDraft((current) => !current || current.options.length >= 8 ? current : {
      ...current,
      options: [...current.options, { label: String.fromCharCode(65 + current.options.length), text: "", order_index: current.options.length }],
    });
  }

  function removeOption(index: number) {
    setDraft((current) => {
      if (!current || current.options.length <= 2) return current;
      const options = current.options.filter((_, optionIndex) => optionIndex !== index).map((option, optionIndex) => ({ ...option, label: String.fromCharCode(65 + optionIndex), order_index: optionIndex }));
      return { ...current, options, correct_answer: typeof current.correct_answer === "string" && options.some((option) => option.label === current.correct_answer) ? current.correct_answer : null };
    });
  }

  async function run(operation: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    try {
      await operation();
      clearError("operation");
    } catch (reason) {
      reportError("operation", reason, "题库操作失败");
    } finally {
      setBusy(false);
    }
  }

  async function changePage(nextPage: number) {
    if (busy || nextPage < 1 || nextPage > totalPages) return;
    setBusy(true);
    try {
      await loadQuestions(undefined, batchId, nextPage);
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    if (!draft) return;
    await run(async () => {
      const saved = await saveQuestion(draft);
      setDraft(clone(saved));
      setActiveId(saved.id);
      await loadQuestions(saved.id);
    });
  }

  async function rerun() {
    if (!draft || !window.confirm("确认重新运行单题 OCR？当前题目字段将被识别结果替换。")) return;
    await run(async () => {
      try {
        const saved = await rerunQuestion(draft.id);
        setDraft(clone(saved));
        await loadQuestions(saved.id);
      } catch (reason) {
        await loadQuestions();
        throw reason;
      }
    });
  }

  async function removeQuestion() {
    if (!draft || !window.confirm("确认删除这道题？用户原始截图不会被删除。")) return;
    await run(async () => {
      await deleteQuestion(draft.id);
      await loadQuestions();
      await loadMetadata();
    });
  }

  async function removeBatch() {
    const targetBatchId = batchId || draft?.batch_id;
    const matchingDraft = draft && draft.batch_id === targetBatchId ? draft : null;
    const targetBatchName = batches.find((batch) => batch.id === targetBatchId)?.name
      ?? matchingDraft?.batch_name
      ?? "所选批次";
    if (!targetBatchId || !window.confirm(`确认删除整个批次“${targetBatchName}”？该操作不可撤销，但不会影响用户原始截图目录。`)) return;
    setNotice("");
    await run(async () => {
      const result = await deleteBatch(targetBatchId);
      if (result.cleanup_pending) {
        setNotice("批次已删除，内部文件清理将在下次启动继续。");
      }
      setBatchId("");
      await loadMetadata();
      await loadQuestions(undefined, "", 1);
    });
  }

  const error = Object.values(errors).join("；");

  if (!page && !error) return <p role="status">正在读取正式题库…</p>;

  return (
    <section className="library-workspace" aria-labelledby="library-title">
      <div className="library-heading">
        <div><span className="progress-kicker">QUESTION LIBRARY</span><h3 id="library-title">题库管理</h3></div>
        <strong>{page?.total ?? 0} 道可用题目</strong>
      </div>

      <div className="library-filters" aria-label="题库筛选">
        <label>课程<select aria-label="课程筛选" value={courseId} onChange={(event) => { setCourseId(event.target.value); setCategoryId(""); setBatchId(""); }}><option value="">全部</option>{courses.map((course) => <option key={course.id} value={course.id}>{course.name}</option>)}</select></label>
        <label>分类<select aria-label="分类筛选" value={categoryId} onChange={(event) => { setCategoryId(event.target.value); setBatchId(""); }}><option value="">全部</option>{filterCategories.map((category) => <option key={category.id} value={category.id}>{category.path}</option>)}</select></label>
        <label>批次<select aria-label="批次筛选" value={batchId} onChange={(event) => setBatchId(event.target.value)}><option value="">全部</option>{batches.map((batch) => <option key={batch.id} value={batch.id}>{batch.name}（{batch.question_count}）</option>)}</select></label>
        <label>题型<select aria-label="题型筛选" value={questionType} onChange={(event) => setQuestionType(event.target.value)}><option value="">全部</option><option value="single_choice">单选题</option><option value="true_false">判断题</option></select></label>
        <label className="library-search">搜索 OCR 文字<input aria-label="搜索 OCR 文字" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="题干或 OCR 文字" /></label>
      </div>

      <div className="library-view-toolbar" aria-label="题目视图模式">
        <button type="button" className={viewMode === "list" ? "active" : ""} onClick={() => setViewMode("list")}>列表视图</button>
        <button type="button" className={viewMode === "thumbnail" ? "active" : ""} onClick={() => setViewMode("thumbnail")}>缩略图视图</button>
        {batchId && <button type="button" disabled={busy} onClick={removeBatch}>删除筛选批次</button>}
      </div>

      {error && <p className="import-warning" role="alert">{error}</p>}
      {!error && notice && <p className="import-warning" role="alert">{notice}</p>}
      {page?.items.length === 0 ? <p className="library-empty">当前筛选条件下没有正式题目。</p> : (
        <div className="library-layout">
          <div className="library-list-panel">
            <aside className={`library-question-grid ${viewMode}-view`} data-testid="library-question-grid" aria-label="正式题目列表">
              {page?.items.map((question) => <button type="button" key={question.id} className={question.id === activeId ? "active" : ""} onClick={() => selectQuestion(question.id)}>
                {viewMode === "thumbnail" && question.parts[0] && <img src={question.parts[0].crop_url} alt="" />}
                <span>{question.question_type === "true_false" ? "判断" : "单选"}</span>
                <strong>{question.stem}</strong>
                <small>{question.category_path} · {question.batch_name}</small>
                <small>{question.source_display_number ?? "来源题号未识别"}{question.in_wrong_book ? " · 错题本" : ""}</small>
              </button>)}
            </aside>
            <nav className="library-pagination" aria-label="题库分页">
              <button type="button" disabled={busy || currentPage <= 1} onClick={() => void changePage(currentPage - 1)}>上一页</button>
              <span aria-live="polite">第 {currentPage} 页，共 {totalPages} 页</span>
              <button type="button" disabled={busy || currentPage >= totalPages} onClick={() => void changePage(currentPage + 1)}>下一页</button>
            </nav>
          </div>

          {draft && <article className="library-detail" aria-label="题目详情与编辑">
            <div className="library-badges"><span>{draft.course_name}</span><span>{draft.category_path}</span>{draft.in_wrong_book && <strong>在错题本</strong>}</div>
            <dl className="library-identifiers"><div><dt>内部 UUID</dt><dd><code>{draft.internal_uuid}</code></dd></div><div><dt>来源题号</dt><dd>{draft.source_display_number ?? "未识别"}</dd></div></dl>
            <div className="library-images">
              <section><h4>题目裁切图</h4>{draft.parts.map((part, index) => <img key={part.id} src={part.crop_url} alt={`题目裁切图 ${index + 1}`} />)}</section>
              <section><h4>来源截图</h4>{draft.parts.length > 0 ? draft.parts.map((part) => <img key={`${part.id}-${part.source_image_id}`} src={part.source_url} alt={`来源截图 ${draft.source_image_name}`} />) : <p>没有图片片段</p>}</section>
            </div>
            <div className="library-fields">
              <label>题目分类<select aria-label="题目分类" value={draft.category_id ?? ""} onChange={(event) => setField("category_id", event.target.value || null)}><option value="">未分类</option>{editCategories.map((category) => <option key={category.id} value={category.id}>{category.path}</option>)}</select></label>
              <label>编辑题干<textarea aria-label="编辑题干" value={draft.stem} onChange={(event) => setField("stem", event.target.value)} /></label>
              <label>题型<select aria-label="题型" value={draft.question_type} onChange={(event) => changeQuestionType(event.target.value as LibraryQuestion["question_type"])}><option value="single_choice">单选题</option><option value="true_false">判断题</option></select></label>
              <fieldset className="option-editor"><legend>选项</legend>{draft.options.map((option, index) => <div key={`${index}-${option.label}`}><input className="option-label-input" aria-label={`选项 ${option.label} 字母`} value={option.label} maxLength={1} onChange={(event) => updateOption(index, "label", event.target.value)} /><input aria-label={`选项 ${option.label} 文本`} value={option.text} onChange={(event) => updateOption(index, "text", event.target.value)} /><button type="button" disabled={draft.options.length <= 2} onClick={() => removeOption(index)}>删除</button></div>)}<button type="button" disabled={draft.options.length >= 8} onClick={addOption}>新增选项</button></fieldset>
              {draft.question_type === "single_choice" ? <label>正确答案<select aria-label="正确答案" value={typeof draft.correct_answer === "string" ? draft.correct_answer : ""} onChange={(event) => setField("correct_answer", event.target.value)}>{draft.options.map((option) => <option key={option.label} value={option.label}>{option.label}</option>)}</select></label> : <><label>判断答案<select aria-label="判断答案" value={draft.correct_answer === true ? "true" : "false"} onChange={(event) => setField("correct_answer", event.target.value === "true")}><option value="true">正确</option><option value="false">错误</option></select></label><label>正确映射<select aria-label="正确选项映射" value={draft.true_label ?? ""} onChange={(event) => setField("true_label", event.target.value || null)}>{draft.options.map((option) => <option key={option.label}>{option.label}</option>)}</select></label><label>错误映射<select aria-label="错误选项映射" value={draft.false_label ?? ""} onChange={(event) => setField("false_label", event.target.value || null)}>{draft.options.map((option) => <option key={option.label}>{option.label}</option>)}</select></label></>}
              <label>显示模式<select aria-label="显示模式" value={draft.display_mode} onChange={(event) => setField("display_mode", event.target.value)}><option value="text">文字</option><option value="text_first">文字优先</option><option value="image_preferred">图片优先</option><option value="image_first">图片优先（人工）</option></select></label>
              <label>原始 OCR<textarea aria-label="原始 OCR" value={draft.raw_ocr_text} readOnly /></label>
              {draft.edited_ocr_text && <label>人工 OCR<textarea aria-label="人工 OCR" value={draft.edited_ocr_text} readOnly /></label>}
              <section className="library-explanation"><h4>解析</h4><p>{draft.explanation || "暂无解析"}</p></section>
            </div>
            <div className="library-actions"><button type="button" disabled={busy} onClick={save}>保存题目</button><button type="button" disabled={busy} onClick={rerun}>单题重新 OCR</button><button type="button" disabled={busy} onClick={removeQuestion}>删除单题</button><button type="button" disabled={busy} onClick={removeBatch}>删除当前批次</button></div>
          </article>}
        </div>
      )}
    </section>
  );
}
