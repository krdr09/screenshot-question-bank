import { useEffect, useMemo, useRef, useState } from "react";

import { getBatches, getCourses, type BatchSummary, type CourseSummary } from "../library/api";
import { answerText } from "../practice/answerText";
import { getWrongQuestions, masterWrongQuestion, restoreWrongQuestion, type WrongQuestion } from "./api";


type Props = { onStartPractice: (questionIds: string[]) => void };

export function WrongBookWorkspace({ onStartPractice }: Props) {
  const [courses, setCourses] = useState<CourseSummary[]>([]);
  const [batches, setBatches] = useState<BatchSummary[]>([]);
  const [items, setItems] = useState<WrongQuestion[]>([]);
  const [courseId, setCourseId] = useState("");
  const [categoryId, setCategoryId] = useState("");
  const [batchId, setBatchId] = useState("");
  const [includeMastered, setIncludeMastered] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [detailId, setDetailId] = useState<string | null>(null);
  const [scale, setScale] = useState(100);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const loadVersion = useRef(0);

  useEffect(() => {
    void getCourses().then(setCourses).catch((value: Error) => setError(value.message));
  }, []);
  useEffect(() => {
    void getBatches(courseId, categoryId).then(setBatches).catch((value: Error) => setError(value.message));
  }, [courseId, categoryId]);

  const load = async () => {
    const version = ++loadVersion.current;
    setError("");
    try {
      const loaded = await getWrongQuestions({
        course_id: courseId,
        category_id: categoryId,
        batch_id: batchId,
        include_mastered: includeMastered,
      });
      if (version !== loadVersion.current) return;
      setItems(loaded);
      const selectableIds = new Set(loaded.filter((item) => !item.mastered).map((item) => item.question_id));
      setSelectedIds((current) => new Set([...current].filter((id) => selectableIds.has(id))));
      setDetailId((current) => loaded.some((item) => item.question_id === current) ? current : loaded[0]?.question_id ?? null);
    } catch (value) {
      if (version !== loadVersion.current) return;
      setError((value as Error).message);
    }
  };
  useEffect(() => { void load(); }, [courseId, categoryId, batchId, includeMastered]);

  const categories = useMemo(() => courses.find((course) => course.id === courseId)?.categories ?? [], [courses, courseId]);
  const detail = items.find((item) => item.question_id === detailId) ?? items[0] ?? null;
  const toggle = (id: string) => setSelectedIds((current) => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  const changeMastery = async (item: WrongQuestion) => {
    setBusyId(item.question_id);
    setError("");
    try {
      if (item.mastered) await restoreWrongQuestion(item.question_id);
      else await masterWrongQuestion(item.question_id);
      setSelectedIds((current) => {
        const next = new Set(current);
        if (!item.mastered) next.delete(item.question_id);
        return next;
      });
      await load();
    } catch (value) {
      setError((value as Error).message);
    } finally {
      setBusyId(null);
    }
  };

  return (
    <section className="wrong-book-workspace" aria-labelledby="wrong-book-title">
      <div className="wrong-book-heading"><div><p className="eyebrow">WRONG BOOK</p><h2 id="wrong-book-title">错题本</h2></div><button type="button" disabled={!selectedIds.size} onClick={() => onStartPractice([...selectedIds])}>刷选中错题</button></div>
      {error && <p role="alert" className="form-error">{error}</p>}
      <div className="wrong-book-filters">
        <label>课程<select aria-label="错题课程" value={courseId} onChange={(event) => { setCourseId(event.target.value); setCategoryId(""); setBatchId(""); }}><option value="">全部课程</option>{courses.map((course) => <option value={course.id} key={course.id}>{course.name}</option>)}</select></label>
        <label>分类/章节<select aria-label="错题分类/章节" value={categoryId} onChange={(event) => { setCategoryId(event.target.value); setBatchId(""); }}><option value="">全部分类</option>{categories.map((category) => <option value={category.id} key={category.id}>{category.path}</option>)}</select></label>
        <label>导入批次<select aria-label="错题导入批次" value={batchId} onChange={(event) => setBatchId(event.target.value)}><option value="">全部批次</option>{batches.map((batch) => <option value={batch.id} key={batch.id}>{batch.name}</option>)}</select></label>
        <label className="wrong-mastered-filter"><input aria-label="包含已掌握" type="checkbox" checked={includeMastered} onChange={(event) => setIncludeMastered(event.target.checked)} />包含已掌握</label>
      </div>
      <p>共 {items.length} 道错题；默认隐藏已掌握题目。</p>
      <div className="wrong-book-layout">
        <div className="wrong-book-list" aria-label="错题列表">
          {items.map((item) => <article key={item.question_id} className={item.question_id === detail?.question_id ? "active" : ""}>
            <label><input type="checkbox" aria-label={`选择 ${item.stem}`} checked={selectedIds.has(item.question_id)} disabled={item.mastered} onChange={() => toggle(item.question_id)} /><span>{item.stem}</span></label>
            <p>{item.course_name} · {item.category_path} · 批次位置 {item.batch_position}</p><strong>累计答错 {item.error_count} 次</strong>
            <div><button type="button" onClick={() => { setDetailId(item.question_id); setScale(100); }}>查看详情</button><button type="button" disabled={busyId === item.question_id} onClick={() => void changeMastery(item)}>{item.mastered ? "恢复到错题本" : "标记已掌握"}</button></div>
          </article>)}
          {!items.length && <p>当前筛选下没有错题。</p>}
        </div>
        {detail && <article className="wrong-book-detail">
          <div><span className="status-chip">{detail.mastered ? "已掌握" : "待巩固"}</span><span>{detail.question_type === "single_choice" ? "单选题" : "判断题"}</span></div>
          <h3>{detail.stem}</h3>
          <ol>{detail.options.map((option) => <li key={option.label}><strong>{option.label}.</strong> {option.text}</li>)}</ol>
          <p><strong>正确答案：{answerText(detail.correct_answer, detail.options)}</strong></p>
          <p>{detail.explanation ?? "暂无解析"}</p>
          {!!detail.image_parts.length && <section className="wrong-book-images" aria-label="错题裁切图片">
            <div className="practice-image-controls"><button type="button" aria-label="缩小错题图片" onClick={() => setScale((value) => Math.max(50, value - 10))}>−</button><span>{scale}%</span><button type="button" aria-label="放大错题图片" onClick={() => setScale((value) => Math.min(200, value + 10))}>＋</button></div>
            {[...detail.image_parts].sort((a, b) => a.part_index - b.part_index).map((part, index) => <div className="practice-image-viewport" key={part.url}><img style={{ width: `${scale}%` }} src={part.url} alt={`错题裁切图 ${index + 1}`} /></div>)}
          </section>}
        </article>}
      </div>
    </section>
  );
}
