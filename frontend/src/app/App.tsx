import { useState } from "react";

import { BackupWorkspace } from "../features/backups/BackupWorkspace";
import { ImportWizard } from "../features/imports/ImportWizard";
import { LibraryWorkspace } from "../features/library/LibraryWorkspace";
import { LAST_PRACTICE_SESSION_KEY, PracticeWorkspace } from "../features/practice/PracticeWorkspace";
import { PracticeHistory } from "../features/practice/PracticeHistory";
import { ReviewWorkspace } from "../features/review/ReviewWorkspace";
import { WrongBookWorkspace } from "../features/wrongBook/WrongBookWorkspace";

const features = [
  ["01", "本地导入", "图片只复制到本机应用数据目录"],
  ["02", "异常复核", "只处理低置信度或结构异常的题目"],
  ["03", "题库管理", "筛选、查看和修正已确认题目"],
  ["04", "开始刷题", "按范围出题、自动判分并保留进度"],
  ["05", "错题巩固", "累计错误次数并支持掌握与恢复"],
  ["06", "可靠备份", "验证哈希并在恢复失败时保留当前数据"],
] as const;

export function App() {
  const [reviewOpen, setReviewOpen] = useState(false);
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [practiceOpen, setPracticeOpen] = useState(() => Boolean(localStorage.getItem(LAST_PRACTICE_SESSION_KEY)));
  const [wrongBookOpen, setWrongBookOpen] = useState(false);
  const [backupOpen, setBackupOpen] = useState(false);
  const [practiceQuestionIds, setPracticeQuestionIds] = useState<string[]>([]);
  const [practiceKey, setPracticeKey] = useState(0);
  const [historyOpen, setHistoryOpen] = useState(false);
  const startSelectedPractice = (questionIds: string[]) => {
    localStorage.removeItem(LAST_PRACTICE_SESSION_KEY);
    setPracticeQuestionIds(questionIds);
    setPracticeKey((value) => value + 1);
    setPracticeOpen(true);
    setWrongBookOpen(false);
  };
  const resumePractice = (sessionId: string) => {
    localStorage.setItem(LAST_PRACTICE_SESSION_KEY, sessionId);
    setPracticeQuestionIds([]);
    setPracticeKey((value) => value + 1);
    setPracticeOpen(true);
  };
  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="#top" aria-label="返回首页">
          <span className="brand-mark">题</span>
          <span>本地题库</span>
        </a>
        <nav aria-label="主导航">
          <a href="#workflow">工作流程</a>
          <a href="#library-center">题库管理</a>
          <a href="#practice-center">刷题</a>
          <a href="#wrong-book-center">错题本</a>
          <a href="#backup-center">备份恢复</a>
          <a href="#privacy">数据安全</a>
        </nav>
      </header>

      <main id="top">
        <section className="hero" aria-labelledby="hero-title">
          <p className="eyebrow">WINDOWS · LOCAL FIRST</p>
          <h1 id="hero-title">截图题库 OCR 与刷题系统</h1>
          <p className="hero-copy">
            把连续课堂截图整理成可检查、可编辑、可反复练习的个人题库。
            <strong>所有题目与图片仅保存在本机</strong>。
          </p>
          <p className="phase-status">阶段 10 · 自动化测试与最终验收</p>
        </section>

        <section id="workflow" className="feature-grid" aria-label="工作流程">
          {features.map(([number, title, description]) => (
            <article className="feature-card" key={number}>
              <span>{number}</span>
              <h2>{title}</h2>
              <p>{description}</p>
            </article>
          ))}
        </section>

        <ImportWizard />

        <section className="review-launcher" id="review-center" aria-labelledby="review-center-title">
          <div>
            <p className="eyebrow">MANUAL REVIEW</p>
            <h2 id="review-center-title">待检查中心</h2>
            <p>随时重新打开所有批次的异常题、低置信度题与未决关系，不依赖本次导入页面状态。</p>
          </div>
          {!reviewOpen && <button type="button" onClick={() => setReviewOpen(true)}>打开待检查页面</button>}
          {reviewOpen && <ReviewWorkspace />}
        </section>

        <section className="library-launcher" id="library-center" aria-labelledby="library-center-title">
          <div>
            <p className="eyebrow">QUESTION LIBRARY</p>
            <h2 id="library-center-title">正式题库</h2>
            <p>筛选、搜索、查看和修改已经确认的题目，并安全管理导入批次。</p>
          </div>
          {!libraryOpen && <button type="button" onClick={() => setLibraryOpen(true)}>打开题库管理</button>}
          {libraryOpen && <LibraryWorkspace />}
        </section>

        <section className="practice-launcher" id="practice-center" aria-labelledby="practice-center-title">
          <div>
            <p className="eyebrow">PERSISTED PRACTICE</p>
            <h2 id="practice-center-title">刷题与自动判分</h2>
            <p>从正式题库选择题目，使用逐题反馈或整套反馈，并在刷新后保留服务端会话。</p>
          </div>
          {!practiceOpen && <button type="button" onClick={() => { localStorage.removeItem(LAST_PRACTICE_SESSION_KEY); setPracticeQuestionIds([]); setPracticeKey((value) => value + 1); setPracticeOpen(true); }}>开始刷题</button>}
          {practiceOpen && <PracticeWorkspace key={practiceKey} initialQuestionIds={practiceQuestionIds} onOpenWrongBook={() => setWrongBookOpen(true)} />}
          <button type="button" onClick={() => setHistoryOpen((value) => !value)}>{historyOpen ? "收起练习历史" : "查看练习历史"}</button>
          {historyOpen && <PracticeHistory onResume={resumePractice} />}
        </section>

        <section className="wrong-book-launcher" id="wrong-book-center" aria-labelledby="wrong-book-center-title">
          <div>
            <p className="eyebrow">WRONG QUESTION REVIEW</p>
            <h2 id="wrong-book-center-title">错题巩固</h2>
            <p>查看真实答错次数、答案和解析，标记已掌握，或选择错题重新练习。</p>
          </div>
          {!wrongBookOpen && <button type="button" onClick={() => setWrongBookOpen(true)}>打开错题本</button>}
          {wrongBookOpen && <WrongBookWorkspace onStartPractice={startSelectedPractice} />}
        </section>

        <section className="backup-launcher" id="backup-center" aria-labelledby="backup-center-title">
          <div>
            <p className="eyebrow">VERIFIED LOCAL BACKUP</p>
            <h2 id="backup-center-title">备份与恢复</h2>
            <p>创建本机完整备份，验证数据库与图片哈希，并在明确确认后安全恢复。</p>
          </div>
          {!backupOpen && <button type="button" onClick={() => setBackupOpen(true)}>打开备份与恢复</button>}
          {backupOpen && <BackupWorkspace />}
        </section>

        <section id="privacy" className="privacy-note">
          <strong>离线边界</strong>
          <span>系统不包含在线 OCR、云账户、遥测、广告或第三方上传。</span>
        </section>
      </main>
    </div>
  );
}
