from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from PIL import Image, ImageDraw

from app.ocr.base import OcrBox, OcrPageResult, Point


ScenarioName = Literal["A", "B", "C", "D", "E"]
WIDTH = 720
HEIGHT = 1280


@dataclass(frozen=True)
class ScenarioFixture:
    scenario_name: ScenarioName
    root: Path
    import_directories: list[Path]
    image_paths: list[Path]
    manifest_path: Path
    expectations: dict[str, Any]

    def summary(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario_name,
            "root": str(self.root),
            "import_directories": [
                str(path.relative_to(self.root)) for path in self.import_directories
            ],
            "images": [str(path.relative_to(self.root)) for path in self.image_paths],
            "manifest": str(self.manifest_path.relative_to(self.root)),
            "expectations": self.expectations,
        }


@dataclass(frozen=True)
class _Page:
    image_path: Path
    result: OcrPageResult


def _box(text: str, y: int, confidence: float = 0.99) -> OcrBox:
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


def _render_page(
    image_path: Path,
    lines: list[str],
    *,
    fixture: str,
    raw_output: dict[str, Any] | None = None,
) -> _Page:
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (WIDTH, HEIGHT), "#f5f1e7")
    draw = ImageDraw.Draw(image)
    boxes: list[OcrBox] = []
    for index, line in enumerate(lines):
        y = 80 + index * 90
        draw.rounded_rectangle((32, y - 16, 688, y + 54), radius=10, fill="white")
        draw.text((48, y), f"synthetic-{fixture}-{index + 1}", fill="#17221d")
        boxes.append(_box(line, y))
    draw.point((len(lines), len(fixture)), fill=(len(lines) * 17 % 255, 31, 73))
    image.save(image_path, format="PNG")
    return _Page(
        image_path=image_path,
        result=OcrPageResult(
            page_width=WIDTH,
            page_height=HEIGHT,
            boxes=boxes,
            raw_output={"engine": "mock", "fixture": fixture, **(raw_output or {})},
        ),
    )


def _cross_pages(directory: Path, fixture: str) -> list[_Page]:
    overlap_height = 360
    overlap = Image.new("RGB", (WIDTH, overlap_height), "#f5f1e7")
    overlap_draw = ImageDraw.Draw(overlap)
    for y in range(0, overlap_height, 30):
        overlap_draw.rounded_rectangle(
            (36 + y // 3, y + 3, 650 - y // 4, y + 19),
            radius=5,
            fill=(35, 85 + y // 6, 105),
        )

    first = Image.new("RGB", (WIDTH, HEIGHT), "#f5f1e7")
    second = Image.new("RGB", (WIDTH, HEIGHT), "#f5f1e7")
    first.paste(overlap, (0, HEIGHT - overlap_height))
    second.paste(overlap, (0, 0))
    ImageDraw.Draw(first).rectangle((40, 380, 680, 700), outline="#174d36", width=4)
    ImageDraw.Draw(second).rectangle((40, 500, 680, 850), outline="#8f5f13", width=4)
    first_path = directory / f"合成-{fixture}-跨图-前.png"
    second_path = directory / f"合成-{fixture}-跨图-后.png"
    directory.mkdir(parents=True, exist_ok=True)
    first.save(first_path, format="PNG")
    second.save(second_path, format="PNG")
    return [
        _Page(
            image_path=first_path,
            result=OcrPageResult(
                page_width=WIDTH,
                page_height=HEIGHT,
                boxes=[
                    _box("1.[单选题]", 340),
                    _box("跨截图合成题的前半部分", 430),
                    _box("题干继续", 1000),
                    _box("连接标记", 1120),
                ],
                raw_output={
                    "engine": "mock",
                    "fixture": fixture,
                    "relationship": "cross-first",
                },
            ),
        ),
        _Page(
            image_path=second_path,
            result=OcrPageResult(
                page_width=WIDTH,
                page_height=HEIGHT,
                boxes=[
                    _box("题干继续", 30),
                    _box("连接标记", 140),
                    _box("A. 甲", 520),
                    _box("B. 乙", 620),
                    _box("正确答案:A", 760),
                ],
                raw_output={
                    "engine": "mock",
                    "fixture": fixture,
                    "relationship": "cross-second",
                },
            ),
        ),
    ]


def _scenario_a(root: Path) -> tuple[list[Path], list[_Page], dict[str, Any]]:
    directory = root / "正常导入"
    pages = [
        _render_page(
            directory / "合成-A-01.png",
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
            fixture="A-1",
        ),
        _render_page(
            directory / "合成-A-02.png",
            [
                "2.[单选题]",
                "两项中选择乙",
                "A. 甲",
                "B. 乙",
                "正确答案:B",
            ],
            fixture="A-2",
        ),
        _render_page(
            directory / "合成-A-03.png",
            [
                "3.[判断题]",
                "太阳从东方升起",
                "A. 错误",
                "B. 正确",
                "正确答案:B",
            ],
            fixture="A-3",
        ),
    ]
    return [directory], pages, {"question_types": ["single_choice", "single_choice", "true_false"], "answers": ["E", "B", True]}


def _scenario_b(root: Path) -> tuple[list[Path], list[_Page], dict[str, Any]]:
    directory = root / "异常审核"
    page = _render_page(
        directory / "合成-B-缺少答案.png",
        ["1.[单选题]", "这道合成题缺少答案", "A. 甲", "B. 乙"],
        fixture="B-1",
    )
    return [directory], [page], {"manual_answer": "A", "anomaly": "MISSING_ANSWER"}


def _scenario_c(root: Path) -> tuple[list[Path], list[_Page], dict[str, Any]]:
    directory = root / "跨截图"
    pages = _cross_pages(directory, "C")
    return [directory], pages, {"merged_answer": "A", "image_parts": 2}


def _scenario_d(root: Path) -> tuple[list[Path], list[_Page], dict[str, Any]]:
    first_dir = root / "重复题号批次一"
    second_dir = root / "重复题号批次二"
    first = _render_page(
        first_dir / "合成-D-批次一.png",
        ["1.[单选题]", "批次一独立题目", "A. 一", "B. 二", "正确答案:A"],
        fixture="D-1",
    )
    second = _render_page(
        second_dir / "合成-D-批次二.png",
        ["1.[单选题]", "批次二独立题目", "A. 三", "B. 四", "正确答案:B"],
        fixture="D-2",
    )
    return [first_dir, second_dir], [first, second], {"source_number": "1", "answers": ["A", "B"]}


def _scenario_e(root: Path) -> tuple[list[Path], list[_Page], dict[str, Any]]:
    directory = root / "备份恢复"
    pages = _cross_pages(directory, "E")
    pages.append(
        _render_page(
            directory / "合成-E-独立题.png",
            [
                "2.[单选题]",
                "恢复后仍可继续作答",
                "A. 否",
                "B. 是",
                "正确答案:B",
            ],
            fixture="E-3",
        )
    )
    return [directory], pages, {"merged_answer": "A", "independent_answer": "B", "image_parts": 2}


def _write_manifest(path: Path, pages: list[_Page]) -> None:
    results: dict[str, dict[str, Any]] = {}
    for page in pages:
        digest = hashlib.sha256(page.image_path.read_bytes()).hexdigest()
        if digest in results:
            raise ValueError("E2E 合成图片产生了重复 SHA-256")
        results[digest] = page.result.model_dump(
            mode="json",
            exclude_computed_fields=True,
        )
    path.write_text(
        json.dumps(
            {"schema_version": 1, "results_by_sha256": results},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def generate_e2e_scenario(name: str, output_dir: Path) -> ScenarioFixture:
    normalized = name.upper()
    if normalized not in {"A", "B", "C", "D", "E"}:
        raise ValueError("E2E 场景只支持 A、B、C、D、E")
    scenario_name: ScenarioName = normalized  # type: ignore[assignment]
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise ValueError("E2E 场景输出目录必须为空")
    builders = {
        "A": _scenario_a,
        "B": _scenario_b,
        "C": _scenario_c,
        "D": _scenario_d,
        "E": _scenario_e,
    }
    import_directories, pages, expectations = builders[scenario_name](output_dir)
    manifest_path = output_dir / "fixture.json"
    _write_manifest(manifest_path, pages)
    return ScenarioFixture(
        scenario_name=scenario_name,
        root=output_dir,
        import_directories=import_directories,
        image_paths=[page.image_path for page in pages],
        manifest_path=manifest_path,
        expectations=expectations,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成阶段 10 E2E 合成截图")
    parser.add_argument("--scenario", choices=["A", "B", "C", "D", "E"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    fixture = generate_e2e_scenario(arguments.scenario, arguments.output)
    print(json.dumps(fixture.summary(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
