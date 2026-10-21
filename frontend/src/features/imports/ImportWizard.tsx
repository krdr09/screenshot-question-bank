import { useMemo, useState } from "react";

import { submitImportMapping, type ImportJobProgress } from "./api";
import { ImportProgress } from "./ImportProgress";
import {
  mapSelectedFiles,
  type ImportMapping,
  type MappingResult,
} from "./folderMapping";

const emptyResult: MappingResult = { mappings: [], ignoredCount: 0 };

export function ImportWizard() {
  const [selection, setSelection] = useState<MappingResult>(emptyResult);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [status, setStatus] = useState("");
  const [jobs, setJobs] = useState<ImportJobProgress[]>([]);

  const canSubmit = useMemo(
    () =>
      selection.mappings.length > 0 &&
      selection.mappings.every(
        (mapping) => mapping.courseName.trim() && mapping.batchName.trim(),
      ),
    [selection],
  );

  function acceptFiles(files: File[]) {
    setSelection(mapSelectedFiles(files));
    setStatus("");
  }

  function updateMapping(
    key: string,
    field: "courseName" | "categoryPath" | "batchName",
    value: string,
  ) {
    setSelection((current) => ({
      ...current,
      mappings: current.mappings.map((mapping) =>
        mapping.key === key ? { ...mapping, [field]: value } : mapping,
      ),
    }));
  }

  async function submit() {
    if (!canSubmit || isSubmitting) return;
    setIsSubmitting(true);
    setStatus("正在创建导入任务并复制图片…");
    try {
      const createdJobs: ImportJobProgress[] = [];
      for (const mapping of selection.mappings) {
        const created = await submitImportMapping(mapping);
        createdJobs.push(created);
        setJobs((current) => {
          const withoutSameJob = current.filter((job) => job.id !== created.id);
          return [...withoutSameJob, created];
        });
      }
      setStatus(`已创建 ${createdJobs.length} 个导入批次，共上传 ${selection.mappings.reduce((sum, item) => sum + item.files.length, 0)} 张图片。`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "导入失败，请查看本地日志");
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <section id="import" className="import-wizard" aria-labelledby="import-title">
      <div className="import-heading">
        <div>
          <p className="eyebrow">LOCAL IMPORT</p>
          <h2 id="import-title">导入截图文件夹</h2>
        </div>
        <p>根目录不会作为课程名；所有图片先复制到本机数据目录，原文件不会被修改。</p>
      </div>

      <label
        className="folder-picker"
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault();
          acceptFiles([...event.dataTransfer.files]);
        }}
      >
        <span>选择截图文件夹</span>
        <small>支持 PNG、JPG、JPEG、WEBP；也可拖放文件或文件夹到这里</small>
        <input
          type="file"
          aria-label="选择截图文件夹"
          accept=".png,.jpg,.jpeg,.webp"
          multiple
          {...{ webkitdirectory: "", directory: "" }}
          onChange={(event) => acceptFiles([...event.currentTarget.files ?? []])}
        />
      </label>

      {selection.ignoredCount > 0 && (
        <p className="import-warning">已忽略 {selection.ignoredCount} 个不支持或路径不安全的文件。</p>
      )}

      {selection.mappings.length > 0 && (
        <div className="mapping-list">
          {selection.mappings.map((mapping, index) => (
            <MappingEditor
              key={mapping.key}
              mapping={mapping}
              index={index}
              onChange={updateMapping}
            />
          ))}
          <div className="import-actions">
            <span>
              共 {selection.mappings.reduce((sum, item) => sum + item.files.length, 0)} 张图片
            </span>
            <button type="button" disabled={!canSubmit || isSubmitting} onClick={submit}>
              {isSubmitting ? "正在导入…" : "确认分类并创建导入任务"}
            </button>
          </div>
        </div>
      )}

      {status && <p className="import-status" role="status">{status}</p>}
      {jobs.length > 0 && (
        <section className="progress-list" aria-label="导入处理进度">
          {jobs.map((job) => <ImportProgress key={job.id} initial={job} />)}
        </section>
      )}
    </section>
  );
}

type MappingEditorProps = {
  mapping: ImportMapping;
  index: number;
  onChange: (
    key: string,
    field: "courseName" | "categoryPath" | "batchName",
    value: string,
  ) => void;
};

function MappingEditor({ mapping, index, onChange }: MappingEditorProps) {
  const suffix = index === 0 ? "" : ` ${index + 1}`;
  return (
    <fieldset className="mapping-card">
      <legend>分类映射 {index + 1}</legend>
      <label>
        <span>课程</span>
        <input
          aria-label={`课程${suffix}`}
          value={mapping.courseName}
          onChange={(event) => onChange(mapping.key, "courseName", event.target.value)}
        />
      </label>
      <label>
        <span>分类/章节</span>
        <input
          aria-label={`分类/章节${suffix}`}
          value={mapping.categoryPath}
          onChange={(event) => onChange(mapping.key, "categoryPath", event.target.value)}
        />
      </label>
      <label>
        <span>批次</span>
        <input
          aria-label={`批次${suffix}`}
          value={mapping.batchName}
          onChange={(event) => onChange(mapping.key, "batchName", event.target.value)}
        />
      </label>
      <div className="mapping-summary">
        <strong>{mapping.files.length} 张图片</strong>
        <span>{mapping.sourceRelativePath || "普通图片文件夹"}</span>
      </div>
      <details>
        <summary>查看文件列表</summary>
        <ul>
          {mapping.files.slice(0, 20).map((entry) => (
            <li key={entry.relativePath}>{entry.relativePath}</li>
          ))}
        </ul>
      </details>
    </fieldset>
  );
}
