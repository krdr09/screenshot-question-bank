from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.services.relationships.overlap import OverlapScore


@dataclass(frozen=True, slots=True)
class CrossScreenshotAssessment:
    decision: str
    confidence: float
    merged_text_preview: str
    continuation_mode: str
    requires_confirmation: bool = True


_NON_BLOCKING_SECONDARY_FLAGS = frozenset({"LOW_OCR_CONFIDENCE"})


def _same_nonempty(first: str | None, second: str | None) -> bool:
    if first is None or second is None:
        return False
    left = first.strip()
    right = second.strip()
    return bool(left) and left == right


def assess_cross_screenshot(
    first_flags: set[str],
    second_flags: set[str],
    overlap: OverlapScore,
    first_text: str,
    second_text: str,
    settings: Settings,
    *,
    first_display_number: str | None = None,
    second_display_number: str | None = None,
    first_question_type: str | None = None,
    second_question_type: str | None = None,
) -> CrossScreenshotAssessment | None:
    first_is_cut = "TRUNCATED_BOTTOM" in first_flags
    flag_continuation = bool(
        {"TRUNCATED_TOP", "MISSING_TITLE"} & second_flags
    )
    repeated_title_continuation = (
        _same_nonempty(first_display_number, second_display_number)
        and _same_nonempty(first_question_type, second_question_type)
    )
    second_is_continuation = flag_continuation or repeated_title_continuation
    existing_boundary_continuation = first_is_cut and second_is_continuation
    incomplete_repeated_title = (
        repeated_title_continuation
        and "MISSING_ANSWER" in first_flags
        and not (second_flags - _NON_BLOCKING_SECONDARY_FLAGS)
    )
    if (
        not (existing_boundary_continuation or incomplete_repeated_title)
        or overlap.combined_score < settings.overlap_candidate_threshold
    ):
        return None
    confidence = overlap.combined_score * 0.8 + 0.2
    if existing_boundary_continuation:
        continuation_mode = (
            "boundary_flag" if flag_continuation else "repeated_title"
        )
    else:
        continuation_mode = "repeated_title_incomplete"
    return CrossScreenshotAssessment(
        decision="suggested",
        confidence=min(1.0, confidence),
        merged_text_preview=f"{first_text.rstrip()}\n{second_text.lstrip()}",
        continuation_mode=continuation_mode,
    )
