from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

from app.ocr.base import OcrBox, OcrPageResult, Point


@dataclass(frozen=True)
class SyntheticOcrPage:
    image_path: Path
    ocr_result: OcrPageResult


def _box(text: str, y: int, confidence: float = 0.98) -> OcrBox:
    return OcrBox(
        text=text,
        polygon=[
            Point(x=48, y=y),
            Point(x=672, y=y),
            Point(x=672, y=y + 44),
            Point(x=48, y=y + 44),
        ],
        confidence=confidence,
    )


def generate_normal_screenshot_set(output_dir: Path) -> list[SyntheticOcrPage]:
    output_dir.mkdir(parents=True, exist_ok=True)
    page_lines = [
        ["Question 1", "A. Alpha", "B. Beta", "Correct answer: A"],
        ["Question 2", "A. True", "B. False", "Correct answer: B"],
        ["Question 3", "A. One", "B. Two", "C. Three", "Analysis: fixture"],
    ]
    pages: list[SyntheticOcrPage] = []
    for page_number, lines in enumerate(page_lines, start=1):
        image = Image.new("RGB", (720, 1280), "#f7f4ec")
        draw = ImageDraw.Draw(image)
        boxes = []
        for line_number, line in enumerate(lines):
            y = 100 + line_number * 90
            draw.rounded_rectangle((32, y - 18, 688, y + 56), radius=12, fill="white")
            draw.text((48, y), line, fill="#17221d")
            boxes.append(_box(line, y))
        image_path = output_dir / f"normal-{page_number:02d}.png"
        image.save(image_path, format="PNG")
        pages.append(
            SyntheticOcrPage(
                image_path=image_path,
                ocr_result=OcrPageResult(
                    page_width=720,
                    page_height=1280,
                    boxes=boxes,
                    raw_output={"engine": "mock", "fixture_page": page_number},
                ),
            )
        )
    return pages


def generate_parsing_screenshot_set(output_dir: Path) -> list[SyntheticOcrPage]:
    output_dir.mkdir(parents=True, exist_ok=True)
    page_lines = [
        [
            "1.[单选题]",
            "请选择第五项",
            "A. 甲",
            "B. 乙",
            "C. 丙",
            "D. 丁",
            "E. 戊",
            "正确答案:E",
            "答案解析",
            "第五项是戊",
        ],
        [
            "2.[判断题]",
            "太阳从东方升起",
            "A. 错误",
            "B. 正确",
            "正确答案:B",
        ],
        ["1.[单选题]", "这是一道异常题", "A. 甲", "B. 乙"],
    ]
    pages: list[SyntheticOcrPage] = []
    for page_number, lines in enumerate(page_lines, start=1):
        image = Image.new("RGB", (720, 1280), "#f5f1e7")
        draw = ImageDraw.Draw(image)
        boxes: list[OcrBox] = []
        for line_number, line in enumerate(lines):
            y = 80 + line_number * 90
            draw.rounded_rectangle((32, y - 16, 688, y + 54), radius=10, fill="white")
            draw.text((48, y), f"fixture {page_number}-{line_number + 1}", fill="#17221d")
            boxes.append(_box(line, y))
        image_path = output_dir / f"parsing-{page_number:02d}.png"
        image.save(image_path, format="PNG")
        pages.append(
            SyntheticOcrPage(
                image_path=image_path,
                ocr_result=OcrPageResult(
                    page_width=720,
                    page_height=1280,
                    boxes=boxes,
                    raw_output={"engine": "mock", "parsing_fixture": page_number},
                ),
            )
        )
    return pages


def generate_relationship_screenshot_set(output_dir: Path) -> list[SyntheticOcrPage]:
    """生成重叠跨图一组、近似重复一组；文件名不表达真实顺序。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    width, height = 720, 1280
    overlap_height = 360
    overlap = Image.new("RGB", (width, overlap_height), "#f5f1e7")
    overlap_draw = ImageDraw.Draw(overlap)
    for y in range(0, overlap_height, 30):
        overlap_draw.rounded_rectangle(
            (36 + y // 3, y + 3, 650 - y // 4, y + 19),
            radius=5,
            fill=(35, 85 + y // 6, 105),
        )

    first = Image.new("RGB", (width, height), "#f5f1e7")
    second = Image.new("RGB", (width, height), "#f5f1e7")
    first.paste(overlap, (0, height - overlap_height))
    second.paste(overlap, (0, 0))
    ImageDraw.Draw(first).rectangle((40, 380, 680, 700), outline="#174d36", width=4)
    ImageDraw.Draw(second).rectangle((40, 500, 680, 850), outline="#8f5f13", width=4)
    first_path = output_dir / "cross-random-z.png"
    second_path = output_dir / "cross-random-a.png"
    first.save(first_path)
    second.save(second_path)
    cross_pages = [
        SyntheticOcrPage(
            image_path=first_path,
            ocr_result=OcrPageResult(
                page_width=width,
                page_height=height,
                boxes=[
                    _box("1.[单选题]", 340),
                    _box("跨截图题干前半部分", 430),
                    _box("题干继续", 1000),
                    _box("最后一行", 1120),
                ],
                raw_output={"engine": "mock", "relationship": "cross-first"},
            ),
        ),
        SyntheticOcrPage(
            image_path=second_path,
            ocr_result=OcrPageResult(
                page_width=width,
                page_height=height,
                boxes=[
                    _box("题干继续", 30),
                    _box("最后一行", 140),
                    _box("A. 甲", 520),
                    _box("B. 乙", 620),
                    _box("正确答案:A", 760),
                ],
                raw_output={"engine": "mock", "relationship": "cross-second"},
            ),
        ),
    ]

    duplicate_pages: list[SyntheticOcrPage] = []
    duplicate_lines = ["7.[单选题]", "同批重复题", "A. 甲", "B. 乙", "正确答案:A"]
    for index, name in enumerate(("duplicate-left.png", "duplicate-right.png")):
        image = Image.new("RGB", (width, height), "#f5f1e7")
        draw = ImageDraw.Draw(image)
        draw.point((index, 0), fill=(index * 80, 0, 0))
        boxes: list[OcrBox] = []
        for line_index, line in enumerate(duplicate_lines):
            y = 180 + line_index * 100
            draw.rounded_rectangle((32, y - 16, 688, y + 54), radius=10, fill="white")
            draw.text((48, y), f"same-{line_index}", fill="#17221d")
            boxes.append(_box(line, y))
        image_path = output_dir / name
        image.save(image_path)
        duplicate_pages.append(
            SyntheticOcrPage(
                image_path=image_path,
                ocr_result=OcrPageResult(
                    page_width=width,
                    page_height=height,
                    boxes=boxes,
                    raw_output={"engine": "mock", "relationship": "duplicate", "copy": index},
                ),
            )
        )
    return [*cross_pages, *duplicate_pages]
