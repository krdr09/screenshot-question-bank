from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw

from app.ocr.paddle import PaddleOcrEngine


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="验证本机 PaddleOCR 能够完成真实初始化和推理")
    parser.add_argument("--device", choices=("auto", "cpu", "gpu"), default="cpu")
    return parser.parse_args()


def run_check(
    engine: PaddleOcrEngine,
    requested_device: Literal["auto", "cpu", "gpu"],
) -> int:
    with tempfile.TemporaryDirectory(prefix="question-practice-ocr-check-") as temporary:
        image_path = Path(temporary) / "ocr-readiness.png"
        image = Image.new("RGB", (640, 180), "white")
        drawing = ImageDraw.Draw(image)
        drawing.rectangle((16, 16, 624, 164), outline="black", width=3)
        drawing.text((42, 65), "OCR READY 123", fill="black")
        image.save(image_path, format="PNG")

        result = engine.recognize(image_path)
        if result.page_width != 640 or result.page_height != 180:
            raise RuntimeError("OCR 返回的页面尺寸与生成图片不一致")
        if not result.boxes:
            raise RuntimeError("OCR CPU 就绪检查没有识别到任何文本")
        if requested_device == "cpu" and engine.device != "cpu":
            raise RuntimeError(f"要求 CPU 自检但实际设备为 {engine.device}")
        print(
            f"OCR CPU 就绪检查通过：device={engine.device}, "
            f"boxes={len(result.boxes)}, image={result.page_width}x{result.page_height}"
        )
    return 0


def main() -> int:
    args = parse_args()
    try:
        engine = PaddleOcrEngine(requested_device=args.device)
        return run_check(engine, args.device)
    except Exception as error:
        print(f"OCR CPU 就绪检查失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
