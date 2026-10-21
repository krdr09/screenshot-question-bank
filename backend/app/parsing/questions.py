from __future__ import annotations

import re
from statistics import mean
from typing import Literal

from pydantic import BaseModel, Field

from app.ocr.base import OcrBox, OcrPageResult, Point
from app.parsing.normalization import normalize_ocr_text


TITLE_PATTERN = re.compile(
    r"^\s*(?P<number>\d+)\s*[.、]\s*\[\s*(?P<type>单选题|判断题|多选题|填空题|简答题)\s*\]"
)
OPTION_PATTERN = re.compile(
    r"^\s*(?:\(\s*(?P<paren_label>[A-H])\s*\)|(?P<plain_label>[A-H])\s*[.、:])\s*(?P<text>.+)$",
    re.IGNORECASE,
)
OPTION_LABEL_ONLY_PATTERN = re.compile(
    r"^\s*(?:\(\s*(?P<paren_label>[A-H])\s*\)|(?P<plain_label>[A-H])\s*[.、:])\s*$",
    re.IGNORECASE,
)
ANSWER_MARKER_PATTERN = re.compile(r"^正确答案(?:\s*:\s*(?P<answer>.*))?$")
MY_ANSWER_PATTERN = re.compile(r"^我的答案(?:\s*:\s*(?P<answer>.*))?$")
SCORE_PATTERN = re.compile(r"^得分(?:\s*:|$)")
EXPLANATION_PATTERN = re.compile(
    r"^(?:答案解析|解析)(?:\s*:\s*(?P<text>.*))?$"
)
VIEW_EXPLANATION_PATTERN = re.compile(r"^查看解析$")
EXPLANATION_ANSWER_SUMMARY_PATTERN = re.compile(
    r"^答案(?:\s*:\s*(?P<answer>.+))$"
)
ANSWER_FEEDBACK_BADGES = frozenset({"√", "✓", "✔", "×", "✗"})

TRUE_TEXTS = {"正确", "对", "是", "√", "✓"}
FALSE_TEXTS = {"错误", "错", "否", "×", "✗", "x"}


class AnomalyFlag(BaseModel):
    code: str
    severity: Literal["warning", "error"]
    message: str


class ParsedOption(BaseModel):
    label: str
    text: str
    order_index: int


class ParsedQuestion(BaseModel):
    source_display_number: str | None
    question_type: Literal["single_choice", "true_false", "unsupported"]
    stem: str
    options: list[ParsedOption]
    correct_answer: bool | str | None
    source_correct_answer_raw: str | None
    true_label: str | None = None
    false_label: str | None = None
    explanation: str | None
    raw_ocr_text: str
    normalized_ocr_text: str
    ocr_confidence: float = Field(ge=0, le=1)
    parse_confidence: float = Field(ge=0, le=1)
    display_mode: Literal["text", "image_preferred"]
    anomaly_flags: list[AnomalyFlag]
    source_y_start: int
    source_y_end: int


def _top(box: OcrBox) -> int:
    return round(min(point.y for point in box.polygon))


def _add_flag(flags: list[AnomalyFlag], code: str, message: str) -> None:
    if any(flag.code == code for flag in flags):
        return
    flags.append(AnomalyFlag(code=code, severity="error", message=message))


def _contiguous(labels: list[str]) -> bool:
    if not labels:
        return True
    expected = [chr(ord(labels[0]) + index) for index in range(len(labels))]
    return labels == expected


def _truth_kind(value: str) -> bool | None:
    normalized = normalize_ocr_text(value).strip().lower()
    if normalized in {item.lower() for item in TRUE_TEXTS}:
        return True
    if normalized in {item.lower() for item in FALSE_TEXTS}:
        return False
    return None


def _strict_answer_token(value: str) -> str | None:
    normalized = normalize_ocr_text(value).strip()
    if re.fullmatch(r"[A-H]", normalized, re.IGNORECASE):
        return normalized.upper()
    if _truth_kind(normalized) is not None:
        return normalized.casefold()
    return None


def _box_bounds(box: OcrBox) -> tuple[float, float, float, float]:
    return (
        min(point.x for point in box.polygon),
        min(point.y for point in box.polygon),
        max(point.x for point in box.polygon),
        max(point.y for point in box.polygon),
    )


def _is_safe_split_option_value(value: str) -> bool:
    normalized = normalize_ocr_text(value).strip()
    if not normalized:
        return False
    return not (
        TITLE_PATTERN.match(normalized)
        or OPTION_PATTERN.match(normalized)
        or OPTION_LABEL_ONLY_PATTERN.match(normalized)
        or ANSWER_MARKER_PATTERN.match(normalized)
        or MY_ANSWER_PATTERN.match(normalized)
        or SCORE_PATTERN.match(normalized)
        or EXPLANATION_PATTERN.match(normalized)
        or VIEW_EXPLANATION_PATTERN.match(normalized)
        or EXPLANATION_ANSWER_SUMMARY_PATTERN.match(normalized)
    )


def _is_right_aligned_split_option_value(
    page_width: int,
    marker_box: OcrBox,
    value_box: OcrBox,
    value: str,
) -> bool:
    if not _is_safe_split_option_value(value):
        return False

    marker_left, marker_top, _, marker_bottom = _box_bounds(marker_box)
    _, value_top, value_right, value_bottom = _box_bounds(value_box)
    if marker_left < page_width * 0.75 or value_right > marker_left:
        return False

    marker_height = marker_bottom - marker_top
    value_height = value_bottom - value_top
    if marker_height <= 0 or value_height <= 0:
        return False
    overlap_y = max(
        0.0,
        min(marker_bottom, value_bottom) - max(marker_top, value_top),
    )
    return overlap_y / min(marker_height, value_height) >= 0.80


def _is_left_stacked_split_option_value(
    page_width: int,
    page_height: int,
    marker_box: OcrBox,
    value_box: OcrBox,
    value: str,
) -> bool:
    if not _is_safe_split_option_value(value):
        return False

    marker_left, marker_top, marker_right, marker_bottom = _box_bounds(marker_box)
    value_left, value_top, value_right, value_bottom = _box_bounds(value_box)
    marker_width = marker_right - marker_left
    marker_height = marker_bottom - marker_top
    value_width = value_right - value_left
    value_height = value_bottom - value_top
    if min(marker_width, marker_height, value_width, value_height) <= 0:
        return False
    if marker_right > page_width * 0.25 or value_right > page_width * 0.35:
        return False

    horizontal_gap = value_left - marker_right
    vertical_gap = value_top - marker_bottom
    return (
        0 <= horizontal_gap <= page_width * 0.10
        and 0 < vertical_gap <= page_height * 0.03
    )


def _floating_note_ui_indices(
    boxes: list[OcrBox],
    *,
    page_width: int,
    page_height: int,
) -> set[int]:
    ignored: set[int] = set()
    note_candidates = [
        (index, box)
        for index, box in enumerate(boxes)
        if normalize_ocr_text(box.text).strip() == "写笔记"
    ]
    icon_candidates = [
        (index, box)
        for index, box in enumerate(boxes)
        if normalize_ocr_text(box.text).strip() == "G"
    ]

    for note_index, note_box in note_candidates:
        note_left, note_top, note_right, _ = _box_bounds(note_box)
        if note_left < page_width * 0.75:
            continue

        matches: list[tuple[float, float, int]] = []
        note_center_x = (note_left + note_right) / 2
        for icon_index, icon_box in icon_candidates:
            if icon_index in ignored:
                continue
            icon_left, _, icon_right, icon_bottom = _box_bounds(icon_box)
            if icon_left < page_width * 0.75:
                continue
            gap_y = note_top - icon_bottom
            if not 0 <= gap_y <= page_height * 0.03:
                continue
            overlap_x = max(
                0.0,
                min(note_right, icon_right) - max(note_left, icon_left),
            )
            narrower_width = min(note_right - note_left, icon_right - icon_left)
            if narrower_width <= 0 or overlap_x < narrower_width * 0.50:
                continue
            icon_center_x = (icon_left + icon_right) / 2
            matches.append((gap_y, abs(note_center_x - icon_center_x), icon_index))

        if not matches:
            continue
        _, _, icon_index = min(matches)
        ignored.update({icon_index, note_index})

    return ignored


def _answer_feedback_ui_indices(
    boxes: list[OcrBox],
    *,
    page_width: int,
) -> set[int]:
    ignored: set[int] = set()
    marker_candidates = [
        (index, box)
        for index, box in enumerate(boxes)
        if MY_ANSWER_PATTERN.match(normalize_ocr_text(box.text).strip())
    ]
    badge_candidates = [
        (index, box)
        for index, box in enumerate(boxes)
        if normalize_ocr_text(box.text).strip() in ANSWER_FEEDBACK_BADGES
    ]

    pair_candidates: list[tuple[float, float, int, int]] = []
    for badge_index, badge_box in badge_candidates:
        badge_left, badge_top, _, badge_bottom = _box_bounds(badge_box)
        if badge_left < page_width * 0.75:
            continue
        badge_height = badge_bottom - badge_top
        if badge_height <= 0:
            continue

        badge_center_y = (badge_top + badge_bottom) / 2
        for marker_index, marker_box in marker_candidates:
            _, marker_top, marker_right, marker_bottom = _box_bounds(marker_box)
            marker_height = marker_bottom - marker_top
            if marker_height <= 0 or badge_left <= marker_right:
                continue
            overlap_y = max(
                0.0,
                min(badge_bottom, marker_bottom) - max(badge_top, marker_top),
            )
            overlap_ratio = overlap_y / min(badge_height, marker_height)
            if overlap_ratio < 0.60:
                continue
            marker_center_y = (marker_top + marker_bottom) / 2
            pair_candidates.append(
                (
                    -overlap_ratio,
                    abs(badge_center_y - marker_center_y),
                    badge_index,
                    marker_index,
                )
            )

    used_marker_indices: set[int] = set()
    for _, _, badge_index, marker_index in sorted(pair_candidates):
        if badge_index in ignored or marker_index in used_marker_indices:
            continue
        ignored.add(badge_index)
        used_marker_indices.add(marker_index)

    return ignored


def _is_inline_metadata_value(
    marker_box: OcrBox, value_box: OcrBox, value: str
) -> bool:
    normalized = normalize_ocr_text(value).strip()
    if not re.fullmatch(r"[A-H]", normalized, re.IGNORECASE) and _truth_kind(
        normalized
    ) is None:
        return False

    marker_left, marker_top, marker_right, marker_bottom = _box_bounds(marker_box)
    value_left, value_top, _, value_bottom = _box_bounds(value_box)
    marker_height = max(1.0, marker_bottom - marker_top)
    value_height = max(1.0, value_bottom - value_top)
    overlap_y = max(0.0, min(marker_bottom, value_bottom) - max(marker_top, value_top))
    if overlap_y / min(marker_height, value_height) < 0.60:
        return False

    row_height = max(marker_height, value_height)
    gap_x = value_left - marker_right
    return -row_height <= gap_x <= 2 * row_height


def parse_ocr_page(
    result: OcrPageResult,
    *,
    low_confidence_threshold: float = 0.8,
) -> list[ParsedQuestion]:
    boxes = sorted(result.boxes, key=_top)
    ignored_structure_indices = _floating_note_ui_indices(
        boxes,
        page_width=result.page_width,
        page_height=result.page_height,
    ) | _answer_feedback_ui_indices(
        boxes,
        page_width=result.page_width,
    )
    normalized = [normalize_ocr_text(box.text) for box in boxes]
    titles = [index for index, text in enumerate(normalized) if TITLE_PATTERN.match(text)]
    if not titles:
        return [_unstructured_candidate(result, boxes, normalized)]
    questions: list[ParsedQuestion] = []
    for title_position, start_index in enumerate(titles):
        next_index = titles[title_position + 1] if title_position + 1 < len(titles) else len(boxes)
        questions.append(
            _parse_segment(
                result,
                boxes,
                normalized,
                ignored_structure_indices,
                start_index,
                next_index,
                next_index < len(boxes),
                low_confidence_threshold,
            )
        )
    return questions


def parse_ocr_text(
    raw_text: str,
    confidence: float = 1.0,
    *,
    low_confidence_threshold: float = 0.8,
) -> list[ParsedQuestion]:
    """Reparse already-recognized lines after a manual cross-screenshot merge."""
    lines = [line for line in raw_text.splitlines() if line.strip()]
    boxes: list[OcrBox] = []
    for index, line in enumerate(lines):
        top = 20 + index * 24
        boxes.append(
            OcrBox(
                text=line,
                confidence=confidence,
                polygon=[
                    Point(x=0, y=top),
                    Point(x=1000, y=top),
                    Point(x=1000, y=top + 16),
                    Point(x=0, y=top + 16),
                ],
            )
        )
    return parse_ocr_page(
        OcrPageResult(
            page_width=1000,
            page_height=max(60, 20 + (len(lines) + 1) * 24),
            boxes=boxes,
            raw_output={"source": "merged_ocr_text"},
        ),
        low_confidence_threshold=low_confidence_threshold,
    )


def _unstructured_candidate(
    result: OcrPageResult, boxes: list[OcrBox], normalized: list[str]
) -> ParsedQuestion:
    flags = [
        AnomalyFlag(
            code="MISSING_TITLE", severity="error", message="未识别到题目标题"
        ),
        AnomalyFlag(
            code="TOO_FEW_OPTIONS", severity="error", message="识别到的选项少于 2 个"
        ),
        AnomalyFlag(
            code="MISSING_ANSWER", severity="error", message="未识别到正确答案"
        ),
    ]
    if not boxes:
        flags.append(
            AnomalyFlag(
                code="EMPTY_OCR", severity="error", message="OCR 返回空结果"
            )
        )
    return ParsedQuestion(
        source_display_number=None,
        question_type="unsupported",
        stem="\n".join(normalized),
        options=[],
        correct_answer=None,
        source_correct_answer_raw=None,
        explanation=None,
        raw_ocr_text="\n".join(box.text for box in boxes),
        normalized_ocr_text="\n".join(normalized),
        ocr_confidence=mean(box.confidence for box in boxes) if boxes else 0,
        parse_confidence=0.2,
        display_mode="image_preferred",
        anomaly_flags=flags,
        source_y_start=_top(boxes[0]) if boxes else 0,
        source_y_end=result.page_height,
    )


def _parse_segment(
    result: OcrPageResult,
    boxes: list[OcrBox],
    normalized: list[str],
    ignored_structure_indices: set[int],
    start_index: int,
    end_index: int,
    has_next_title: bool,
    low_confidence_threshold: float,
) -> ParsedQuestion:
    title = TITLE_PATTERN.match(normalized[start_index])
    assert title is not None
    raw_lines = [box.text for box in boxes[start_index:end_index]]
    lines = normalized[start_index:end_index]
    flags: list[AnomalyFlag] = []
    type_text = title.group("type")
    question_type: Literal["single_choice", "true_false", "unsupported"]
    if type_text == "单选题":
        question_type = "single_choice"
    elif type_text == "判断题":
        question_type = "true_false"
    else:
        question_type = "unsupported"
        _add_flag(flags, "UNSUPPORTED_QUESTION_TYPE", "检测到不支持的题型")

    options: list[ParsedOption] = []
    inline_stem = lines[0][title.end() :].strip()
    stem_lines: list[str] = [inline_stem] if inline_stem else []
    answer_raw: str | None = None

    def set_answer(candidate_answer: str) -> None:
        nonlocal answer_raw
        if answer_raw is not None and answer_raw != candidate_answer:
            _add_flag(flags, "CONFLICTING_ANSWERS", "识别到互相冲突的正确答案")
        answer_raw = candidate_answer

    explanation_lines: list[str] = []
    explanation_started = False
    seen_option = False
    metadata_started = False
    pending_metadata: tuple[Literal["my", "answer"], OcrBox] | None = None
    pending_explanation_panel = False
    consumed_split_option_indices: set[int] = set()
    marker_indices: dict[str, list[int]] = {"my": [], "answer": [], "score": []}
    for relative_index, line in enumerate(lines[1:], start=1):
        current_box = boxes[start_index + relative_index]
        absolute_index = start_index + relative_index
        if (
            absolute_index in ignored_structure_indices
            or absolute_index in consumed_split_option_indices
        ):
            continue
        if pending_metadata is not None:
            pending_kind, marker_box = pending_metadata
            pending_metadata = None
            if _is_inline_metadata_value(marker_box, current_box, line):
                if pending_kind == "answer":
                    set_answer(line)
                continue
        if pending_explanation_panel:
            pending_explanation_panel = False
            summary_match = EXPLANATION_ANSWER_SUMMARY_PATTERN.match(line)
            summary_token = (
                _strict_answer_token(summary_match.group("answer"))
                if summary_match
                else None
            )
            answer_token = (
                _strict_answer_token(answer_raw) if answer_raw is not None else None
            )
            if summary_token is not None and summary_token == answer_token:
                explanation_started = True
                continue
        explanation_match = EXPLANATION_PATTERN.match(line)
        if explanation_match:
            metadata_started = True
            explanation_started = True
            if explanation_text := (explanation_match.group("text") or "").strip():
                explanation_lines.append(explanation_text)
            continue
        if explanation_started:
            explanation_lines.append(line)
            continue
        if not line.strip():
            continue
        if VIEW_EXPLANATION_PATTERN.match(line):
            metadata_started = True
            pending_explanation_panel = True
            continue
        my_answer_match = MY_ANSWER_PATTERN.match(line)
        if my_answer_match:
            metadata_started = True
            marker_indices["my"].append(relative_index)
            if not (my_answer_match.group("answer") or "").strip():
                pending_metadata = ("my", current_box)
            continue
        answer_match = ANSWER_MARKER_PATTERN.match(line)
        if answer_match:
            metadata_started = True
            marker_indices["answer"].append(relative_index)
            if candidate_answer := (answer_match.group("answer") or "").strip():
                set_answer(candidate_answer)
            else:
                pending_metadata = ("answer", current_box)
            continue
        if SCORE_PATTERN.match(line):
            metadata_started = True
            marker_indices["score"].append(relative_index)
            continue
        split_option_match = OPTION_LABEL_ONLY_PATTERN.match(line)
        if split_option_match and not metadata_started:
            label = (
                split_option_match.group("paren_label")
                or split_option_match.group("plain_label")
            ).upper()
            label_is_consecutive = not options or ord(label) == ord(
                options[-1].label
            ) + 1
            next_index = next(
                (
                    index
                    for index in range(absolute_index + 1, end_index)
                    if index not in ignored_structure_indices
                ),
                None,
            )
            following_index = (
                next(
                    (
                        index
                        for index in range(next_index + 1, end_index)
                        if index not in ignored_structure_indices
                    ),
                    None,
                )
                if next_index is not None
                else None
            )
            following_option_match = (
                OPTION_PATTERN.match(normalized[following_index])
                if following_index is not None
                else None
            )
            following_split_match = (
                OPTION_LABEL_ONLY_PATTERN.match(normalized[following_index])
                if following_index is not None
                else None
            )
            if following_option_match:
                following_label = (
                    following_option_match.group("paren_label")
                    or following_option_match.group("plain_label")
                )
            elif following_split_match:
                following_label = (
                    following_split_match.group("paren_label")
                    or following_split_match.group("plain_label")
                )
            else:
                following_label = None
            has_consecutive_following_option = (
                following_label is not None
                and ord(following_label.upper()) == ord(label) + 1
            )
            if (
                label_is_consecutive
                and next_index is not None
                and (
                    _is_right_aligned_split_option_value(
                        result.page_width,
                        current_box,
                        boxes[next_index],
                        normalized[next_index],
                    )
                    or (
                        has_consecutive_following_option
                        and _is_left_stacked_split_option_value(
                            result.page_width,
                            result.page_height,
                            current_box,
                            boxes[next_index],
                            normalized[next_index],
                        )
                    )
                )
            ):
                seen_option = True
                options.append(
                    ParsedOption(
                        label=label,
                        text=normalized[next_index].strip(),
                        order_index=len(options),
                    )
                )
                consumed_split_option_indices.add(next_index)
                continue
        option_match = OPTION_PATTERN.match(line)
        if option_match and not metadata_started:
            seen_option = True
            options.append(
                ParsedOption(
                    label=(
                        option_match.group("paren_label")
                        or option_match.group("plain_label")
                    ).upper(),
                    text=option_match.group("text").strip(),
                    order_index=len(options),
                )
            )
            continue
        if metadata_started:
            _add_flag(flags, "UNASSIGNED_TEXT", "存在无法归属的 OCR 文本")
        elif not seen_option:
            stem_lines.append(line)
        else:
            options[-1].text = f"{options[-1].text}\n{line}"

    has_conflicting_answers = any(
        flag.code == "CONFLICTING_ANSWERS" for flag in flags
    )
    if (
        question_type == "true_false"
        and not options
        and answer_raw is not None
        and not has_conflicting_answers
        and _truth_kind(answer_raw) is not None
    ):
        options = [
            ParsedOption(label="A", text="正确", order_index=0),
            ParsedOption(label="B", text="错误", order_index=1),
        ]

    if len(options) < 2:
        _add_flag(flags, "TOO_FEW_OPTIONS", "识别到的选项少于 2 个")
    labels = [option.label for option in options]
    if not _contiguous(labels):
        _add_flag(flags, "NONCONTIGUOUS_OPTIONS", "选项字母不连续")

    correct_answer: bool | str | None = None
    true_label: str | None = None
    false_label: str | None = None
    if answer_raw is None:
        _add_flag(flags, "MISSING_ANSWER", "未识别到正确答案")
    elif has_conflicting_answers:
        correct_answer = None
    elif question_type == "single_choice":
        answer_label = normalize_ocr_text(answer_raw).upper()
        if answer_label not in labels:
            _add_flag(flags, "ANSWER_OUT_OF_RANGE", "正确答案不属于现有选项")
        else:
            correct_answer = answer_label
    elif question_type == "true_false":
        mapped = {option.label: _truth_kind(option.text) for option in options}
        true_labels = [label for label, value in mapped.items() if value is True]
        false_labels = [label for label, value in mapped.items() if value is False]
        if len(true_labels) == 1 and len(false_labels) == 1:
            true_label, false_label = true_labels[0], false_labels[0]
            answer_label = normalize_ocr_text(answer_raw).upper()
            if answer_label in mapped and mapped[answer_label] is not None:
                correct_answer = mapped[answer_label]
            else:
                literal = _truth_kind(answer_raw)
                if literal is None:
                    _add_flag(flags, "TRUE_FALSE_MAPPING_FAILED", "判断题无法映射为正确或错误")
                else:
                    correct_answer = literal
        else:
            _add_flag(flags, "TRUE_FALSE_MAPPING_FAILED", "判断题选项含义无法唯一确认")

    question_boxes = boxes[start_index:end_index]
    average_confidence = mean(box.confidence for box in question_boxes)
    if average_confidence < low_confidence_threshold:
        _add_flag(flags, "LOW_OCR_CONFIDENCE", "OCR 平均置信度低")
    source_y_start = _top(boxes[start_index])
    if source_y_start <= 8:
        _add_flag(flags, "TRUNCATED_TOP", "题目疑似在截图顶部被截断")

    crop_marker_index = min(
        (index for indices in marker_indices.values() for index in indices),
        default=None,
    )
    if crop_marker_index is not None:
        source_y_end = _top(boxes[start_index + crop_marker_index])
    elif has_next_title:
        source_y_end = _top(boxes[end_index])
    else:
        source_y_end = result.page_height
        _add_flag(flags, "TRUNCATED_BOTTOM", "题目疑似在截图底部被截断")

    stem = "\n".join(stem_lines).strip()
    if not stem:
        _add_flag(flags, "MISSING_STEM", "未识别到题干")

    parse_confidence = max(0.0, 1.0 - 0.15 * len(flags))
    return ParsedQuestion(
        source_display_number=title.group("number"),
        question_type=question_type,
        stem=stem,
        options=options,
        correct_answer=correct_answer,
        source_correct_answer_raw=answer_raw,
        true_label=true_label,
        false_label=false_label,
        explanation="\n".join(explanation_lines).strip() or None,
        raw_ocr_text="\n".join(raw_lines),
        normalized_ocr_text="\n".join(lines),
        ocr_confidence=average_confidence,
        parse_confidence=parse_confidence,
        display_mode="image_preferred" if flags else "text",
        anomaly_flags=flags,
        source_y_start=source_y_start,
        source_y_end=source_y_end,
    )
