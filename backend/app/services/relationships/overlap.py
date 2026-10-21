from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

from app.config import Settings
from app.ocr.base import OcrBox, OcrPageResult
from app.parsing.normalization import normalize_ocr_text


STATUS_NOISE_TOKEN = re.compile(
    r"^(?:\d{1,2}:\d{2}|\d{1,3}%|(?:中国)?(?:移动|联通|电信)|[45]g|wi-?fi|wifi|信号|电池|电量|📶|🔋|[▁▂▃▄▅▆▇█]+)$",
    re.IGNORECASE,
)
STATUS_SEPARATOR = re.compile(r"[\s|·•,，;；/_-]+")


def _is_status_noise(line: str) -> bool:
    normalized = re.sub(r"wi-?fi", "wifi", line, flags=re.IGNORECASE)
    tokens = [token for token in STATUS_SEPARATOR.split(normalized) if token]
    return bool(tokens) and all(STATUS_NOISE_TOKEN.fullmatch(token) for token in tokens)


@dataclass(frozen=True, slots=True)
class OverlapScore:
    source_id: str
    target_id: str
    text_score: float
    visual_score: float
    combined_score: float
    matched_text: list[str]
    overlap_ratio: float


def _center_y(box: OcrBox) -> float:
    return sum(point.y for point in box.polygon) / len(box.polygon)


def _normalized_line(text: str) -> str:
    return normalize_ocr_text(text).casefold()


def _region_lines(
    result: OcrPageResult, *, top: bool, band_ratio: float
) -> list[str]:
    boundary = result.page_height * (band_ratio if top else 1 - band_ratio)
    selected = [
        box
        for box in result.boxes
        if (_center_y(box) <= boundary if top else _center_y(box) >= boundary)
    ]
    selected.sort(key=_center_y)
    lines: list[str] = []
    for box in selected:
        line = _normalized_line(box.text)
        if not line or _is_status_noise(line):
            continue
        lines.append(line)
    return lines


def _text_overlap(first: list[str], second: list[str]) -> tuple[float, list[str]]:
    best_score = 0.0
    best_lines: list[str] = []
    for count in range(1, min(len(first), len(second)) + 1):
        suffix = first[-count:]
        prefix = second[:count]
        left = "\n".join(suffix)
        right = "\n".join(prefix)
        if min(len(left), len(right)) < 4:
            continue
        score = SequenceMatcher(None, left, right, autojunk=False).ratio()
        if score > best_score or (score == best_score and count > len(best_lines)):
            best_score = score
            best_lines = suffix
    return best_score, best_lines


def _strip_similarity(first: Image.Image, second: Image.Image, ratio: float) -> float:
    width = min(first.width, second.width)
    height = max(1, round(min(first.height, second.height) * ratio))
    first_strip = first.crop((0, first.height - height, first.width, first.height))
    second_strip = second.crop((0, 0, second.width, height))
    size = (min(320, width), min(240, height))
    first_gray = first_strip.convert("L").resize(size)
    second_gray = second_strip.convert("L").resize(size)
    mean_difference = ImageStat.Stat(ImageChops.difference(first_gray, second_gray)).mean[0]
    return max(0.0, 1.0 - mean_difference / 255.0)


def _visual_overlap(
    first_path: Path, second_path: Path, settings: Settings
) -> tuple[float, float]:
    with Image.open(first_path) as first, Image.open(second_path) as second:
        best_score = 0.0
        best_ratio = settings.overlap_visual_min_ratio
        ratio = settings.overlap_visual_min_ratio
        while ratio <= settings.overlap_visual_max_ratio + 1e-9:
            score = _strip_similarity(first, second, ratio)
            if score > best_score:
                best_score = score
                best_ratio = ratio
            ratio += settings.overlap_visual_ratio_step
    return best_score, best_ratio


def score_directional_overlap(
    first: OcrPageResult,
    second: OcrPageResult,
    first_path: Path,
    second_path: Path,
    settings: Settings,
    *,
    source_id: str = "",
    target_id: str = "",
) -> OverlapScore:
    first_lines = _region_lines(first, top=False, band_ratio=settings.overlap_band_ratio)
    second_lines = _region_lines(second, top=True, band_ratio=settings.overlap_band_ratio)
    text_score, matched_text = _text_overlap(first_lines, second_lines)
    visual_score, ratio = _visual_overlap(first_path, second_path, settings)
    total_weight = settings.overlap_text_weight + settings.overlap_visual_weight
    combined = (
        text_score * settings.overlap_text_weight
        + visual_score * settings.overlap_visual_weight
    ) / total_weight
    return OverlapScore(
        source_id=source_id,
        target_id=target_id,
        text_score=text_score,
        visual_score=visual_score,
        combined_score=combined,
        matched_text=matched_text,
        overlap_ratio=ratio,
    )
