import { useEffect, useState } from "react";

import type { PracticeImagePart } from "./api";


type Props = {
  parts: PracticeImagePart[];
  imagePreferred?: boolean;
};

export function QuestionImageViewer({ parts, imagePreferred = false }: Props) {
  const [open, setOpen] = useState(imagePreferred);
  const [scale, setScale] = useState(100);

  useEffect(() => {
    setOpen(imagePreferred);
    setScale(100);
  }, [imagePreferred, parts]);

  if (!parts.length) return null;
  const adjust = (delta: number) => setScale((current) => Math.min(200, Math.max(50, current + delta)));
  return (
    <section className="practice-image-viewer" aria-label="题目裁切图片">
      {imagePreferred && <p className="practice-image-warning">此题建议结合裁切图片核对，OCR 文字仍保留供参考。</p>}
      <button type="button" className="practice-image-toggle" onClick={() => setOpen((value) => !value)}>
        {open ? "收起题图" : "查看题图"}
      </button>
      {open && (
        <div>
          <div className="practice-image-controls">
            <button type="button" aria-label="缩小题图" onClick={() => adjust(-10)}>−</button>
            <span data-testid="practice-image-scale">{scale}%</span>
            <button type="button" aria-label="放大题图" onClick={() => adjust(10)}>＋</button>
          </div>
          <div className="practice-images" style={{ "--practice-image-scale": scale / 100 } as React.CSSProperties}>
            {[...parts].sort((a, b) => a.part_index - b.part_index).map((part, index) => (
              <div className="practice-image-viewport" key={`${part.part_index}-${part.url}`}>
                <img src={part.url} alt={`题目图片 ${index + 1}`} />
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
