from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from PIL import Image

from app.config import Settings
from app.parsing.normalization import normalize_ocr_text


@dataclass(frozen=True, slots=True)
class DuplicateAssessment:
    relation_type: str
    decision: str
    text_score: float
    hash_distance: int
    confidence: float
    evidence: dict[str, Any]


def normalized_content(value: str) -> str:
    return re.sub(r"\s+", "", normalize_ocr_text(value).casefold())


def perceptual_hash(path: Path) -> int:
    with Image.open(path) as image:
        gray = image.convert("L").resize((9, 8))
        pixels = list(gray.getdata())
    value = 0
    for row in range(8):
        offset = row * 9
        for column in range(8):
            value = (value << 1) | int(
                pixels[offset + column] > pixels[offset + column + 1]
            )
    return value


def hash_distance(first: int, second: int) -> int:
    return (first ^ second).bit_count()


def assess_duplicate(
    first_text: str,
    second_text: str,
    first_image: Path,
    second_image: Path,
    first_answer: str | bool | None,
    second_answer: str | bool | None,
    same_batch: bool,
    settings: Settings,
) -> DuplicateAssessment | None:
    left = normalized_content(first_text)
    right = normalized_content(second_text)
    text_score = SequenceMatcher(None, left, right, autojunk=False).ratio()
    distance = hash_distance(perceptual_hash(first_image), perceptual_hash(second_image))
    answers_match = first_answer == second_answer and first_answer is not None
    answer_conflict = (
        first_answer is not None
        and second_answer is not None
        and first_answer != second_answer
    )
    visually_close = distance <= settings.duplicate_hash_distance
    visually_near = distance <= (
        settings.duplicate_hash_distance + settings.duplicate_hash_review_margin
    )
    text_close = text_score >= settings.duplicate_text_threshold
    near_text = text_score >= (
        settings.duplicate_text_threshold - settings.duplicate_review_margin
    )
    exact_text_match = bool(left) and left == right
    visual_gate_bypassed_for_review = (
        same_batch
        and exact_text_match
        and answers_match
        and not visually_near
    )
    if not near_text or not (
        visually_near or visual_gate_bypassed_for_review
    ):
        return None
    confidence = min(text_score, max(0.0, 1 - distance / 64))
    evidence = {
        "answers_match": answers_match,
        "answer_conflict": answer_conflict,
        "same_batch": same_batch,
        "exact_text_match": exact_text_match,
        "visual_gate_bypassed_for_review": visual_gate_bypassed_for_review,
    }
    if not same_batch:
        return DuplicateAssessment(
            "cross_batch_match",
            "kept_separate",
            text_score,
            distance,
            confidence,
            evidence,
        )
    if text_close and visually_close and answers_match:
        decision = "auto_merged"
    else:
        decision = "review"
    return DuplicateAssessment(
        "same_batch_duplicate",
        decision,
        text_score,
        distance,
        confidence,
        evidence,
    )
