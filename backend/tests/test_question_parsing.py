import pytest

from app.ocr.base import OcrBox, OcrPageResult, Point
from app.parsing.questions import parse_ocr_page


def positioned_box(
    text: str,
    left: int,
    top: int,
    right: int,
    bottom: int,
    confidence: float = 0.98,
) -> OcrBox:
    return OcrBox(
        text=text,
        polygon=[
            Point(x=left, y=top),
            Point(x=right, y=top),
            Point(x=right, y=bottom),
            Point(x=left, y=bottom),
        ],
        confidence=confidence,
    )


def box(text: str, y: int, confidence: float = 0.98) -> OcrBox:
    return positioned_box(text, 20, y, 680, y + 36, confidence)


def page(*lines: str, start_y: int = 80) -> OcrPageResult:
    return OcrPageResult(
        page_width=720,
        page_height=1280,
        boxes=[box(text, start_y + index * 70) for index, text in enumerate(lines)],
        raw_output={"fixture": True},
    )


def floating_note_boxes(
    *,
    icon_text: str = "G",
    note_text: str = "写笔记",
    icon_left: int = 600,
    icon_top: int = 180,
    icon_right: int = 650,
    icon_bottom: int = 220,
    note_left: int = 580,
    note_top: int = 240,
    note_right: int = 680,
    note_bottom: int = 270,
) -> list[OcrBox]:
    return [
        positioned_box(
            icon_text,
            icon_left,
            icon_top,
            icon_right,
            icon_bottom,
            0.50,
        ),
        positioned_box(
            note_text,
            note_left,
            note_top,
            note_right,
            note_bottom,
            0.99,
        ),
    ]


def right_aligned_split_option_boxes(
    label_token: str,
    value: str,
    top: int,
) -> list[OcrBox]:
    return [
        positioned_box(label_token, 600, top, 650, top + 40),
        positioned_box(value, 20, top + 4, 180, top + 44),
    ]


def anomaly_codes(question) -> set[str]:
    return {flag.code for flag in question.anomaly_flags}


def test_parses_single_choice_a_to_e_and_correct_answer() -> None:
    questions = parse_ocr_page(
        page(
            "2．【单选题】",
            "以下哪个选项正确？",
            "A．甲",
            "B、乙",
            "C:丙",
            "(D) 丁",
            "（E）戊",
            "正确答案：E",
        )
    )

    assert len(questions) == 1
    question = questions[0]
    assert question.source_display_number == "2"
    assert question.question_type == "single_choice"
    assert question.stem == "以下哪个选项正确?"
    assert [(option.label, option.text) for option in question.options] == [
        ("A", "甲"),
        ("B", "乙"),
        ("C", "丙"),
        ("D", "丁"),
        ("E", "戊"),
    ]
    assert question.correct_answer == "E"
    assert question.source_correct_answer_raw == "E"
    assert question.anomaly_flags == []


def test_true_false_maps_answer_from_reversed_option_text() -> None:
    question = parse_ocr_page(
        page(
            "3. [判断题]",
            "地球是球体",
            "A. 错误",
            "B. 正确",
            "正确答案:B",
        )
    )[0]

    assert question.question_type == "true_false"
    assert question.correct_answer is True
    assert question.source_correct_answer_raw == "B"
    assert question.true_label == "B"
    assert question.false_label == "A"
    assert question.anomaly_flags == []


@pytest.mark.parametrize(
    ("answer_raw", "expected_answer"),
    [("对", True), ("正确", True), ("错", False), ("错误", False)],
)
def test_true_false_maps_literal_answer_without_source_options(
    answer_raw: str, expected_answer: bool
) -> None:
    answer_marker = f"正确答案:{answer_raw}"
    question = parse_ocr_page(
        page("1.[判断题]", "示例命题", answer_marker)
    )[0]

    assert [
        (option.label, option.text, option.order_index) for option in question.options
    ] == [("A", "正确", 0), ("B", "错误", 1)]
    assert question.correct_answer is expected_answer
    assert question.true_label == "A"
    assert question.false_label == "B"
    assert question.source_correct_answer_raw == answer_raw
    assert question.raw_ocr_text == f"1.[判断题]\n示例命题\n{answer_marker}"
    assert "A. 正确" not in question.raw_ocr_text
    assert "B. 错误" not in question.normalized_ocr_text
    assert "TOO_FEW_OPTIONS" not in anomaly_codes(question)
    assert "TRUE_FALSE_MAPPING_FAILED" not in anomaly_codes(question)


def test_true_false_does_not_fill_partial_source_options() -> None:
    question = parse_ocr_page(
        page("2.[判断题]", "示例命题", "A. 正确", "正确答案:正确")
    )[0]

    assert [(option.label, option.text) for option in question.options] == [
        ("A", "正确")
    ]
    assert question.correct_answer is None
    assert "TOO_FEW_OPTIONS" in anomaly_codes(question)
    assert "TRUE_FALSE_MAPPING_FAILED" in anomaly_codes(question)


def test_true_false_does_not_guess_unknown_literal_without_options() -> None:
    question = parse_ocr_page(
        page("3.[判断题]", "示例命题", "正确答案:未知")
    )[0]

    assert question.options == []
    assert question.correct_answer is None
    assert "TOO_FEW_OPTIONS" in anomaly_codes(question)
    assert "TRUE_FALSE_MAPPING_FAILED" in anomaly_codes(question)


def test_true_false_does_not_fill_options_for_conflicting_literal_answers() -> None:
    question = parse_ocr_page(
        page(
            "4.[判断题]",
            "示例命题",
            "正确答案:对",
            "正确答案:错",
        )
    )[0]

    assert question.options == []
    assert question.correct_answer is None
    assert "CONFLICTING_ANSWERS" in anomaly_codes(question)
    assert "TOO_FEW_OPTIONS" in anomaly_codes(question)


def test_missing_and_out_of_range_answers_are_structured_anomalies() -> None:
    missing = parse_ocr_page(
        page("1.[单选题]", "题干", "A. 甲", "B. 乙")
    )[0]
    out_of_range = parse_ocr_page(
        page("2.[单选题]", "题干", "A. 甲", "B. 乙", "正确答案:C")
    )[0]

    assert "MISSING_ANSWER" in anomaly_codes(missing)
    assert missing.correct_answer is None
    assert "ANSWER_OUT_OF_RANGE" in anomaly_codes(out_of_range)
    assert out_of_range.correct_answer is None


def test_answer_score_and_expanded_explanation_do_not_enter_stem() -> None:
    question = parse_ocr_page(
        page(
            "8、[单选题]",
            "真正的题干",
            "A. 甲",
            "B. 乙",
            "我的答案：A",
            "正确答案：B",
            "得分：0",
            "答案解析",
            "这是已经展开的解析",
        )
    )[0]

    assert question.stem == "真正的题干"
    assert question.explanation == "这是已经展开的解析"
    assert "我的答案" not in question.stem
    assert "得分" not in question.stem


def test_unsupported_type_and_discontinuous_options_require_review() -> None:
    unsupported = parse_ocr_page(
        page("4.[多选题]", "题干", "A. 甲", "B. 乙", "正确答案:A")
    )[0]
    discontinuous = parse_ocr_page(
        page("5.[单选题]", "题干", "A. 甲", "C. 丙", "正确答案:A")
    )[0]

    assert unsupported.question_type == "unsupported"
    assert "UNSUPPORTED_QUESTION_TYPE" in anomaly_codes(unsupported)
    assert "NONCONTIGUOUS_OPTIONS" in anomaly_codes(discontinuous)


def test_question_without_end_marker_at_page_bottom_is_truncated() -> None:
    result = page("9.[单选题]", "题干", "A. 甲", "B. 乙", start_y=1010)
    question = parse_ocr_page(result)[0]

    assert "TRUNCATED_BOTTOM" in anomaly_codes(question)
    assert question.source_y_end == result.page_height
    assert question.display_mode == "image_preferred"


def test_missing_title_and_empty_ocr_become_reviewable_candidates() -> None:
    missing_title = parse_ocr_page(page("零散题干", "A. 甲", "B. 乙"))[0]
    empty = parse_ocr_page(
        OcrPageResult(
            page_width=720,
            page_height=1280,
            boxes=[],
            raw_output={"fixture": "empty"},
        )
    )[0]

    assert "MISSING_TITLE" in anomaly_codes(missing_title)
    assert missing_title.raw_ocr_text == "零散题干\nA. 甲\nB. 乙"
    assert {"MISSING_TITLE", "EMPTY_OCR"} <= anomaly_codes(empty)
    assert empty.display_mode == "image_preferred"


def test_conflicting_answers_do_not_choose_one_silently() -> None:
    question = parse_ocr_page(
        page(
            "10.[单选题]",
            "题干",
            "A. 甲",
            "B. 乙",
            "正确答案:A",
            "正确答案:B",
        )
    )[0]

    assert "CONFLICTING_ANSWERS" in anomaly_codes(question)
    assert question.correct_answer is None


def test_stem_starting_with_letter_a_is_not_misread_as_option() -> None:
    question = parse_ocr_page(
        page(
            "11.[单选题]",
            "A股市场中的说法哪项正确？",
            "A. 甲",
            "B. 乙",
            "正确答案:A",
        )
    )[0]

    assert question.stem == "A股市场中的说法哪项正确?"
    assert [option.label for option in question.options] == ["A", "B"]


def test_low_confidence_threshold_is_configurable_and_inclusive() -> None:
    result = page(
        "12.[单选题]",
        "题干",
        "A. 甲",
        "B. 乙",
        "正确答案:A",
    )
    result.boxes = [
        box(item.text, round(item.polygon[0].y), 0.9) for item in result.boxes
    ]

    below = parse_ocr_page(result, low_confidence_threshold=0.91)[0]
    equal = parse_ocr_page(result, low_confidence_threshold=0.9)[0]

    assert "LOW_OCR_CONFIDENCE" in anomaly_codes(below)
    assert "LOW_OCR_CONFIDENCE" not in anomaly_codes(equal)


def test_extracts_stem_after_title_from_same_ocr_box() -> None:
    question = parse_ocr_page(
        page(
            "1.[单选题] 合并在标题框中的题干",
            "A. 甲",
            "B. 乙",
            "正确答案:A",
        )
    )[0]

    assert question.stem == "合并在标题框中的题干"
    assert "MISSING_STEM" not in anomaly_codes(question)
    assert question.anomaly_flags == []


def test_missing_stem_requires_review() -> None:
    question = parse_ocr_page(
        page("2.[单选题]", "A. 甲", "B. 乙", "正确答案:A")
    )[0]

    assert question.stem == ""
    assert "MISSING_STEM" in anomaly_codes(question)
    assert question.display_mode == "image_preferred"


def test_merges_multiline_option_continuation() -> None:
    question = parse_ocr_page(
        page(
            "3.[单选题]",
            "题干",
            "A. 选项 A 第一行",
            "选项 A 续行",
            "B. 选项 B",
            "正确答案:B",
        )
    )[0]

    assert [(option.label, option.text) for option in question.options] == [
        ("A", "选项 A 第一行\n选项 A 续行"),
        ("B", "选项 B"),
    ]
    assert question.correct_answer == "B"
    assert question.anomaly_flags == []


def test_text_after_metadata_is_unassigned_not_option_continuation() -> None:
    question = parse_ocr_page(
        page(
            "4.[单选题]",
            "题干",
            "A. 甲",
            "B. 乙",
            "正确答案:A",
            "无法归属的界面文本",
        )
    )[0]

    assert question.options[-1].text == "乙"
    assert question.correct_answer == "A"
    assert "UNASSIGNED_TEXT" in anomaly_codes(question)
    assert question.display_mode == "image_preferred"


def test_extracts_explanation_from_same_ocr_box() -> None:
    question = parse_ocr_page(
        page(
            "5.[单选题]",
            "题干",
            "A. 甲",
            "B. 乙",
            "正确答案:A",
            "答案解析: 同框解析正文",
        )
    )[0]

    assert question.explanation == "同框解析正文"
    assert "UNASSIGNED_TEXT" not in anomaly_codes(question)


def test_expanded_view_explanation_panel_captures_body_after_matching_recap() -> None:
    question = parse_ocr_page(
        page(
            "1.[单选题]",
            "示例命题",
            "A. 甲",
            "B. 乙",
            "正确答案:B",
            "查看解析",
            "答案:B",
            "第一行说明",
            "第二行说明",
        )
    )[0]

    assert question.correct_answer == "B"
    assert question.explanation == "第一行说明\n第二行说明"
    assert "查看解析\n答案:B\n第一行说明\n第二行说明" in question.raw_ocr_text
    assert (
        "查看解析\n答案:B\n第一行说明\n第二行说明"
        in question.normalized_ocr_text
    )
    assert "UNASSIGNED_TEXT" not in anomaly_codes(question)
    assert question.source_y_end == 360


def test_collapsed_view_explanation_marker_is_known_ui_metadata() -> None:
    question = parse_ocr_page(
        page(
            "2.[单选题]",
            "示例命题",
            "A. 甲",
            "B. 乙",
            "正确答案:A",
            "查看解析",
        )
    )[0]

    assert question.correct_answer == "A"
    assert question.explanation is None
    assert "UNASSIGNED_TEXT" not in anomaly_codes(question)
    assert question.raw_ocr_text.endswith("查看解析")


def answer_feedback_question(
    badge: str,
    *,
    left: int = 1140,
    top: int = 398,
    right: int = 1220,
    bottom: int = 462,
    extra_badges: list[OcrBox] | None = None,
):
    boxes = [
        positioned_box("5.[单选题]", 20, 20, 240, 60),
        positioned_box("合成题干", 20, 80, 300, 120),
        positioned_box("A. 甲", 20, 140, 220, 180),
        positioned_box("B. 乙", 20, 200, 220, 240),
        positioned_box("C. 丙", 20, 260, 220, 300),
        positioned_box("D. 丁", 20, 320, 220, 360),
        positioned_box(badge, left, top, right, bottom),
        *(extra_badges or []),
        positioned_box("我的答案：A", 20, 403, 300, 460),
        positioned_box("正确答案：C", 20, 480, 300, 540),
        positioned_box("得分：0", 20, 560, 300, 620),
    ]
    return parse_ocr_page(
        OcrPageResult(
            page_width=1320,
            page_height=900,
            boxes=boxes,
            raw_output={"fixture": "answer-feedback-badge-boundary"},
        )
    )[0]


def test_answer_feedback_badge_before_user_answer_does_not_extend_latest_option() -> None:
    question = answer_feedback_question("×")

    assert [(item.label, item.text) for item in question.options] == [
        ("A", "甲"),
        ("B", "乙"),
        ("C", "丙"),
        ("D", "丁"),
    ]
    assert question.correct_answer == "C"
    assert question.source_y_end == 403
    assert "×" in question.raw_ocr_text
    assert "×" in question.normalized_ocr_text
    assert "UNASSIGNED_TEXT" not in anomaly_codes(question)


@pytest.mark.parametrize("badge", ["√", "✓", "✔", "×", "✗"])
def test_same_row_unicode_answer_feedback_badges_are_structurally_ignored(
    badge: str,
) -> None:
    question = answer_feedback_question(badge)

    assert [(item.label, item.text) for item in question.options] == [
        ("A", "甲"),
        ("B", "乙"),
        ("C", "丙"),
        ("D", "丁"),
    ]
    assert question.correct_answer == "C"
    assert question.source_y_end == 403
    assert badge in question.raw_ocr_text
    assert badge in question.normalized_ocr_text
    assert "UNASSIGNED_TEXT" not in anomaly_codes(question)


def test_answer_feedback_pairing_prefers_greatest_overlap_not_input_order() -> None:
    question = answer_feedback_question(
        "✗",
        top=390,
        bottom=430,
        extra_badges=[positioned_box("×", 1140, 400, 1220, 460)],
    )

    assert question.options[-1].text == "丁\n✗"
    assert "×" in question.raw_ocr_text
    assert "✗" in question.raw_ocr_text


@pytest.mark.parametrize(
    ("badge", "left", "top", "right", "bottom"),
    [
        ("×", 500, 398, 580, 462),
        ("×", 1140, 365, 1220, 400),
        ("××", 1140, 398, 1220, 462),
        ("x", 1140, 398, 1220, 462),
        ("X", 1140, 398, 1220, 462),
    ],
)
def test_answer_feedback_near_matches_are_not_silently_discarded(
    badge: str,
    left: int,
    top: int,
    right: int,
    bottom: int,
) -> None:
    question = answer_feedback_question(
        badge,
        left=left,
        top=top,
        right=right,
        bottom=bottom,
    )

    assert badge in question.options[-1].text
    assert badge in question.raw_ocr_text


def test_true_false_answer_glyph_remains_a_legitimate_answer_token() -> None:
    question = parse_ocr_page(
        page("8.[判断题]", "合成命题", "正确答案：×")
    )[0]

    assert question.question_type == "true_false"
    assert question.correct_answer is False
    assert [(item.label, item.text) for item in question.options] == [
        ("A", "正确"),
        ("B", "错误"),
    ]


@pytest.mark.parametrize(
    "lines",
    [
        ("查看解析", "普通界面文本", "第一行说明"),
        ("查看解析", "答案:", "第一行说明"),
        ("查看解析", "答案:AB", "第一行说明"),
        ("查看解析", "答案:A", "第一行说明"),
        ("查看解析", "中间提示", "答案:B", "第一行说明"),
    ],
)
def test_view_explanation_panel_rejects_untrusted_recap(
    lines: tuple[str, ...],
) -> None:
    question = parse_ocr_page(
        page(
            "3.[单选题]",
            "示例命题",
            "A. 甲",
            "B. 乙",
            "正确答案:B",
            *lines,
        )
    )[0]

    assert question.correct_answer == "B"
    assert question.explanation is None
    assert "UNASSIGNED_TEXT" in anomaly_codes(question)


def test_view_explanation_recap_does_not_supply_missing_correct_answer() -> None:
    question = parse_ocr_page(
        page(
            "4.[单选题]",
            "示例命题",
            "A. 甲",
            "B. 乙",
            "查看解析",
            "答案:B",
            "第一行说明",
        )
    )[0]

    assert question.correct_answer is None
    assert question.source_correct_answer_raw is None
    assert question.explanation is None
    assert "MISSING_ANSWER" in anomaly_codes(question)
    assert "UNASSIGNED_TEXT" in anomaly_codes(question)


def test_floating_note_widget_does_not_enter_stem_and_preserves_ocr_evidence() -> None:
    boxes = [
        positioned_box("1.[单选题]", 20, 20, 180, 50),
        positioned_box("示例命题", 20, 80, 200, 110),
        *floating_note_boxes(),
        positioned_box("A. 甲", 20, 320, 150, 350),
        positioned_box("B. 乙", 20, 380, 150, 410),
        positioned_box("正确答案:A", 20, 440, 200, 470),
    ]
    result = OcrPageResult(
        page_width=720,
        page_height=1280,
        boxes=boxes,
        raw_output={"fixture": "floating-note-stem"},
    )

    question = parse_ocr_page(result)[0]

    assert question.stem == "示例命题"
    assert [(option.label, option.text) for option in question.options] == [
        ("A", "甲"),
        ("B", "乙"),
    ]
    assert question.correct_answer == "A"
    assert question.raw_ocr_text.splitlines() == [box.text for box in boxes]
    assert question.normalized_ocr_text.splitlines()[2:4] == ["G", "写笔记"]
    assert question.ocr_confidence == pytest.approx(0.9128571428571428)
    assert question.source_y_end == 440


def test_floating_note_widget_does_not_extend_latest_option() -> None:
    question = parse_ocr_page(
        OcrPageResult(
            page_width=720,
            page_height=1280,
            boxes=[
                positioned_box("2.[单选题]", 20, 20, 180, 50),
                positioned_box("示例命题", 20, 80, 200, 110),
                positioned_box("A. 甲", 20, 140, 150, 170),
                *floating_note_boxes(icon_top=200, icon_bottom=230, note_top=250),
                positioned_box("B. 乙", 20, 320, 150, 350),
                positioned_box("正确答案:B", 20, 380, 200, 410),
            ],
            raw_output={"fixture": "floating-note-option"},
        )
    )[0]

    assert [(option.label, option.text) for option in question.options] == [
        ("A", "甲"),
        ("B", "乙"),
    ]
    assert question.correct_answer == "B"


def test_floating_note_widget_after_metadata_does_not_create_unassigned_text() -> None:
    question = parse_ocr_page(
        OcrPageResult(
            page_width=720,
            page_height=1280,
            boxes=[
                positioned_box("3.[单选题]", 20, 20, 180, 50),
                positioned_box("示例命题", 20, 80, 200, 110),
                positioned_box("A. 甲", 20, 140, 150, 170),
                positioned_box("B. 乙", 20, 200, 150, 230),
                positioned_box("正确答案:A", 20, 260, 200, 290),
                *floating_note_boxes(
                    icon_top=330,
                    icon_bottom=360,
                    note_top=380,
                    note_bottom=410,
                ),
            ],
            raw_output={"fixture": "floating-note-metadata"},
        )
    )[0]

    assert question.correct_answer == "A"
    assert "UNASSIGNED_TEXT" not in anomaly_codes(question)


def test_floating_note_widget_is_transparent_to_split_metadata_pending_state() -> None:
    question = parse_ocr_page(
        OcrPageResult(
            page_width=720,
            page_height=1280,
            boxes=[
                positioned_box("4.[单选题]", 20, 20, 180, 50),
                positioned_box("示例命题", 20, 80, 200, 110),
                positioned_box("A. 甲", 20, 140, 150, 170),
                positioned_box("B. 乙", 20, 200, 150, 230),
                positioned_box("正确答案:", 20, 280, 180, 440),
                *floating_note_boxes(
                    icon_top=300,
                    icon_bottom=330,
                    note_top=350,
                    note_bottom=380,
                ),
                positioned_box("B", 185, 400, 205, 430),
            ],
            raw_output={"fixture": "floating-note-split-metadata"},
        )
    )[0]

    assert question.source_correct_answer_raw == "B"
    assert question.correct_answer == "B"
    assert "UNASSIGNED_TEXT" not in anomaly_codes(question)
    assert "MISSING_ANSWER" not in anomaly_codes(question)
    assert question.source_y_end == 280


def test_floating_note_widget_is_transparent_to_explanation_recap_pending_state() -> None:
    question = parse_ocr_page(
        OcrPageResult(
            page_width=720,
            page_height=1280,
            boxes=[
                positioned_box("5.[单选题]", 20, 20, 180, 50),
                positioned_box("示例命题", 20, 80, 200, 110),
                positioned_box("A. 甲", 20, 140, 150, 170),
                positioned_box("B. 乙", 20, 200, 150, 230),
                positioned_box("正确答案:B", 20, 260, 200, 290),
                positioned_box("查看解析", 20, 320, 180, 350),
                *floating_note_boxes(
                    icon_top=380,
                    icon_bottom=420,
                    note_top=440,
                    note_bottom=470,
                ),
                positioned_box("答案:B", 20, 500, 150, 530),
                positioned_box("第一行说明", 20, 560, 220, 590),
            ],
            raw_output={"fixture": "floating-note-explanation"},
        )
    )[0]

    assert question.correct_answer == "B"
    assert question.explanation == "第一行说明"
    assert "UNASSIGNED_TEXT" not in anomaly_codes(question)


@pytest.mark.parametrize(
    "widget_boxes",
    [
        floating_note_boxes(icon_text="H"),
        floating_note_boxes(note_text="写笔记中"),
        floating_note_boxes(
            icon_left=100,
            icon_right=150,
            note_left=120,
            note_right=220,
        ),
        floating_note_boxes(icon_left=480, icon_right=520),
        floating_note_boxes(icon_top=120, icon_bottom=160, note_top=240),
        floating_note_boxes(
            icon_top=260,
            icon_bottom=290,
            note_top=180,
            note_bottom=220,
        ),
    ],
)
def test_floating_note_near_matches_fail_closed(
    widget_boxes: list[OcrBox],
) -> None:
    question = parse_ocr_page(
        OcrPageResult(
            page_width=720,
            page_height=1280,
            boxes=[
                positioned_box("6.[单选题]", 20, 20, 180, 50),
                positioned_box("示例命题", 20, 80, 200, 110),
                *widget_boxes,
                positioned_box("A. 甲", 20, 320, 150, 350),
                positioned_box("B. 乙", 20, 380, 150, 410),
                positioned_box("正确答案:A", 20, 440, 200, 470),
            ],
            raw_output={"fixture": "floating-note-fail-closed"},
        )
    )[0]

    assert all(widget_box.text in question.stem for widget_box in widget_boxes)


def test_floating_note_pairing_keeps_unpaired_legitimate_g() -> None:
    question = parse_ocr_page(
        OcrPageResult(
            page_width=720,
            page_height=1280,
            boxes=[
                positioned_box("7.[单选题]", 20, 20, 180, 50),
                positioned_box("示例命题", 20, 80, 200, 110),
                *floating_note_boxes(),
                positioned_box("G", 20, 190, 70, 220),
                positioned_box("A. 甲", 20, 320, 150, 350),
                positioned_box("B. 乙", 20, 380, 150, 410),
                positioned_box("正确答案:A", 20, 440, 200, 470),
            ],
            raw_output={"fixture": "floating-note-deterministic-pair"},
        )
    )[0]

    assert question.stem == "示例命题\nG"


def test_parses_right_aligned_split_option_rows_and_preserves_ocr_evidence() -> None:
    boxes = [
        positioned_box("1.[单选题]", 20, 20, 180, 50),
        positioned_box("示例命题", 20, 80, 200, 110),
        *right_aligned_split_option_boxes("A.", "甲", 140),
        *right_aligned_split_option_boxes("B.", "乙", 220),
        *right_aligned_split_option_boxes("C.", "丙", 300),
        positioned_box("D. 丁", 20, 380, 180, 420),
        positioned_box("正确答案:D", 20, 460, 220, 500),
    ]
    result = OcrPageResult(
        page_width=720,
        page_height=1280,
        boxes=boxes,
        raw_output={"fixture": "right-aligned-split-options"},
    )

    question = parse_ocr_page(result)[0]

    assert question.stem == "示例命题"
    assert [(option.label, option.text) for option in question.options] == [
        ("A", "甲"),
        ("B", "乙"),
        ("C", "丙"),
        ("D", "丁"),
    ]
    assert question.correct_answer == "D"
    assert "TOO_FEW_OPTIONS" not in anomaly_codes(question)
    assert question.raw_ocr_text.splitlines() == [box.text for box in boxes]
    assert question.normalized_ocr_text.splitlines()[2:8] == [
        "A.",
        "甲",
        "B.",
        "乙",
        "C.",
        "丙",
    ]
    assert question.ocr_confidence == pytest.approx(0.98)
    assert question.source_y_end == 460


def test_split_option_lookahead_crosses_only_confirmed_floating_note_ui() -> None:
    boxes = [
        positioned_box("2.[单选题]", 20, 20, 180, 50),
        positioned_box("示例命题", 20, 80, 200, 110),
        positioned_box("A.", 600, 140, 650, 300),
        *floating_note_boxes(
            icon_top=180,
            icon_bottom=210,
            note_top=220,
            note_bottom=240,
        ),
        positioned_box("甲", 20, 250, 180, 280),
        positioned_box("B. 乙", 20, 340, 180, 380),
        positioned_box("正确答案:B", 20, 420, 220, 460),
    ]
    question = parse_ocr_page(
        OcrPageResult(
            page_width=720,
            page_height=1280,
            boxes=boxes,
            raw_output={"fixture": "split-option-across-known-ui"},
        )
    )[0]

    assert [(option.label, option.text) for option in question.options] == [
        ("A", "甲"),
        ("B", "乙"),
    ]
    assert question.correct_answer == "B"
    assert "UNASSIGNED_TEXT" not in anomaly_codes(question)
    assert question.raw_ocr_text.splitlines() == [box.text for box in boxes]


@pytest.mark.parametrize("label_token", ["A、", "A:", "(A)", "（A）"])
def test_right_aligned_split_option_reuses_supported_label_forms(
    label_token: str,
) -> None:
    question = parse_ocr_page(
        OcrPageResult(
            page_width=720,
            page_height=600,
            boxes=[
                positioned_box("3.[单选题]", 20, 20, 180, 50),
                positioned_box("示例命题", 20, 80, 200, 110),
                *right_aligned_split_option_boxes(label_token, "甲", 140),
                positioned_box("B. 乙", 20, 220, 180, 260),
                positioned_box("正确答案:B", 20, 300, 220, 340),
            ],
            raw_output={"fixture": "split-option-label-form"},
        )
    )[0]

    assert [(option.label, option.text) for option in question.options] == [
        ("A", "甲"),
        ("B", "乙"),
    ]


@pytest.mark.parametrize(
    "row_boxes",
    [
        [
            positioned_box("A.", 20, 140, 70, 180),
            positioned_box("甲", 100, 144, 180, 184),
        ],
        [
            positioned_box("A.", 600, 140, 650, 180),
            positioned_box("甲", 20, 175, 180, 215),
        ],
        [
            positioned_box("A.", 600, 140, 650, 180),
            positioned_box("甲", 580, 144, 620, 184),
        ],
        [
            positioned_box("A.", 600, 140, 650, 180),
            positioned_box("甲", 660, 144, 700, 184),
        ],
        [
            positioned_box("A", 600, 140, 650, 180),
            positioned_box("甲", 20, 144, 180, 184),
        ],
        [
            positioned_box("甲", 20, 140, 180, 180),
            positioned_box("A.", 600, 144, 650, 184),
        ],
    ],
)
def test_right_aligned_split_option_near_matches_fail_closed(
    row_boxes: list[OcrBox],
) -> None:
    question = parse_ocr_page(
        OcrPageResult(
            page_width=720,
            page_height=600,
            boxes=[
                positioned_box("4.[单选题]", 20, 20, 180, 50),
                positioned_box("示例命题", 20, 80, 200, 110),
                *row_boxes,
                positioned_box("B. 乙", 20, 240, 180, 280),
                positioned_box("正确答案:B", 20, 320, 220, 360),
            ],
            raw_output={"fixture": "split-option-near-match"},
        )
    )[0]

    assert [(option.label, option.text) for option in question.options] == [
        ("B", "乙")
    ]


@pytest.mark.parametrize(
    "next_text",
    [
        "   ",
        "B. 乙",
        "B.",
        "正确答案:B",
        "我的答案:B",
        "得分:1",
        "解析:说明",
        "查看解析",
        "答案:B",
    ],
)
def test_split_option_value_rejects_structural_markers(next_text: str) -> None:
    question = parse_ocr_page(
        OcrPageResult(
            page_width=720,
            page_height=600,
            boxes=[
                positioned_box("5.[单选题]", 20, 20, 180, 50),
                positioned_box("示例命题", 20, 80, 200, 110),
                positioned_box("A.", 600, 140, 650, 220),
                positioned_box(next_text, 20, 144, 180, 184),
            ],
            raw_output={"fixture": "split-option-structural-value"},
        )
    )[0]

    assert "A" not in [option.label for option in question.options]


def test_split_option_does_not_cross_the_next_question_title() -> None:
    questions = parse_ocr_page(
        OcrPageResult(
            page_width=720,
            page_height=600,
            boxes=[
                positioned_box("8.[单选题]", 20, 20, 180, 50),
                positioned_box("第一题干", 20, 80, 200, 110),
                positioned_box("A.", 600, 140, 650, 220),
                positioned_box("9.[单选题]", 20, 144, 180, 184),
                positioned_box("第二题干", 20, 240, 200, 270),
                positioned_box("A. 甲", 20, 300, 180, 340),
                positioned_box("B. 乙", 20, 360, 180, 400),
                positioned_box("正确答案:B", 20, 440, 220, 480),
            ],
            raw_output={"fixture": "split-option-next-title"},
        )
    )

    assert len(questions) == 2
    assert questions[0].options == []
    assert questions[1].source_display_number == "9"


def test_split_option_lookahead_does_not_cross_an_ordinary_semantic_box() -> None:
    question = parse_ocr_page(
        OcrPageResult(
            page_width=720,
            page_height=600,
            boxes=[
                positioned_box("6.[单选题]", 20, 20, 180, 50),
                positioned_box("示例命题", 20, 80, 200, 110),
                positioned_box("A.", 600, 140, 650, 300),
                positioned_box("B. 乙", 20, 180, 180, 220),
                positioned_box("甲", 20, 250, 180, 280),
                positioned_box("正确答案:B", 20, 340, 220, 380),
            ],
            raw_output={"fixture": "split-option-ordinary-barrier"},
        )
    )[0]

    assert "A" not in [option.label for option in question.options]
    assert [(option.label, option.text) for option in question.options] == [
        ("B", "乙\n甲")
    ]


def test_split_option_requires_continuity_after_existing_options() -> None:
    question = parse_ocr_page(
        OcrPageResult(
            page_width=720,
            page_height=600,
            boxes=[
                positioned_box("7.[单选题]", 20, 20, 180, 50),
                positioned_box("示例命题", 20, 80, 200, 110),
                positioned_box("A. 甲", 20, 140, 180, 180),
                *right_aligned_split_option_boxes("C.", "丙", 220),
                positioned_box("B. 乙", 20, 300, 180, 340),
                positioned_box("正确答案:B", 20, 380, 220, 420),
            ],
            raw_output={"fixture": "split-option-noncontiguous"},
        )
    )[0]

    assert [(option.label, option.text) for option in question.options] == [
        ("A", "甲\nC.\n丙"),
        ("B", "乙"),
    ]
    assert question.correct_answer == "B"


def test_parses_left_stacked_split_option_across_floating_note_ui() -> None:
    boxes = [
        positioned_box("1.[单选题]", 20, 20, 180, 50),
        positioned_box("示例命题", 20, 80, 200, 110),
        positioned_box("G", 620, 120, 660, 145),
        positioned_box("A.", 20, 140, 70, 180),
        positioned_box("写笔记", 600, 155, 690, 180),
        positioned_box("0", 100, 205, 130, 245),
        positioned_box("B. 乙", 20, 280, 180, 320),
        positioned_box("C. 丙", 20, 340, 180, 380),
        positioned_box("D. 丁", 20, 400, 180, 440),
        positioned_box("正确答案:A", 20, 480, 220, 520),
    ]
    question = parse_ocr_page(
        OcrPageResult(
            page_width=720,
            page_height=1280,
            boxes=boxes,
            raw_output={"fixture": "left-stacked-split-option"},
        )
    )[0]

    assert question.stem == "示例命题"
    assert [(option.label, option.text) for option in question.options] == [
        ("A", "0"),
        ("B", "乙"),
        ("C", "丙"),
        ("D", "丁"),
    ]
    assert question.correct_answer == "A"
    assert question.anomaly_flags == []
    assert question.raw_ocr_text.splitlines() == [box.text for box in boxes]
    assert "G" in question.normalized_ocr_text
    assert "写笔记" in question.normalized_ocr_text
    assert question.ocr_confidence == pytest.approx(0.98)
    assert question.source_y_end == 480


@pytest.mark.parametrize("label_token", ["A、", "A:", "(A)", "（A）"])
def test_left_stacked_split_option_reuses_supported_label_forms(
    label_token: str,
) -> None:
    question = parse_ocr_page(
        OcrPageResult(
            page_width=720,
            page_height=1280,
            boxes=[
                positioned_box("2.[单选题]", 20, 20, 180, 50),
                positioned_box("示例命题", 20, 80, 200, 110),
                positioned_box(label_token, 20, 140, 70, 180),
                positioned_box("甲", 100, 205, 150, 245),
                positioned_box("B. 乙", 20, 280, 180, 320),
                positioned_box("正确答案:B", 20, 360, 220, 400),
            ],
            raw_output={"fixture": "left-stacked-label-form"},
        )
    )[0]

    assert [(option.label, option.text) for option in question.options] == [
        ("A", "甲"),
        ("B", "乙"),
    ]


@pytest.mark.parametrize(
    "value_box",
    [
        positioned_box("甲", 100, 144, 150, 184),
        positioned_box("甲", 100, 240, 150, 280),
        positioned_box("甲", 200, 205, 250, 245),
        positioned_box("甲", 500, 205, 550, 245),
        positioned_box("甲", 100, 100, 150, 130),
        positioned_box("甲", 100, 205, 150, 205),
    ],
)
def test_left_stacked_split_option_geometry_near_matches_fail_closed(
    value_box: OcrBox,
) -> None:
    question = parse_ocr_page(
        OcrPageResult(
            page_width=720,
            page_height=1280,
            boxes=[
                positioned_box("3.[单选题]", 20, 20, 180, 50),
                positioned_box("示例命题", 20, 80, 200, 110),
                positioned_box("A.", 20, 140, 70, 180),
                value_box,
                positioned_box("B. 乙", 20, 300, 180, 340),
                positioned_box("正确答案:B", 20, 380, 220, 420),
            ],
            raw_output={"fixture": "left-stacked-geometry-guard"},
        )
    )[0]

    assert "A" not in [option.label for option in question.options]


@pytest.mark.parametrize(
    "value",
    [
        "   ",
        "9.[单选题]",
        "B. 乙",
        "B.",
        "正确答案:A",
        "我的答案:A",
        "得分:1",
        "解析:说明",
        "查看解析",
        "答案:A",
    ],
)
def test_left_stacked_split_option_rejects_structural_values(value: str) -> None:
    question = parse_ocr_page(
        OcrPageResult(
            page_width=720,
            page_height=1280,
            boxes=[
                positioned_box("4.[单选题]", 20, 20, 180, 50),
                positioned_box("示例命题", 20, 80, 200, 110),
                positioned_box("A.", 20, 140, 70, 180),
                positioned_box(value, 100, 205, 180, 245),
                positioned_box("B. 乙", 20, 280, 180, 320),
            ],
            raw_output={"fixture": "left-stacked-structural-value"},
        )
    )[0]

    assert "A" not in [option.label for option in question.options]


@pytest.mark.parametrize("following", ["C. 丙", "普通续行", "正确答案:A"])
def test_left_stacked_split_option_requires_direct_consecutive_witness(
    following: str,
) -> None:
    question = parse_ocr_page(
        OcrPageResult(
            page_width=720,
            page_height=1280,
            boxes=[
                positioned_box("5.[单选题]", 20, 20, 180, 50),
                positioned_box("示例命题", 20, 80, 200, 110),
                positioned_box("A.", 20, 140, 70, 180),
                positioned_box("甲", 100, 205, 150, 245),
                positioned_box(following, 20, 280, 180, 320),
            ],
            raw_output={"fixture": "left-stacked-witness"},
        )
    )[0]

    assert "A" not in [option.label for option in question.options]


def test_left_stacked_split_option_does_not_cross_ordinary_box() -> None:
    question = parse_ocr_page(
        OcrPageResult(
            page_width=720,
            page_height=1280,
            boxes=[
                positioned_box("6.[单选题]", 20, 20, 180, 50),
                positioned_box("示例命题", 20, 80, 200, 110),
                positioned_box("A.", 20, 140, 70, 180),
                positioned_box("普通中间框", 400, 185, 520, 205),
                positioned_box("甲", 100, 210, 150, 250),
                positioned_box("B. 乙", 20, 300, 180, 340),
                positioned_box("正确答案:B", 20, 380, 220, 420),
            ],
            raw_output={"fixture": "left-stacked-ordinary-barrier"},
        )
    )[0]

    assert "A" not in [option.label for option in question.options]


def test_left_stacked_split_option_does_not_cross_unpaired_ui_token() -> None:
    question = parse_ocr_page(
        OcrPageResult(
            page_width=720,
            page_height=1280,
            boxes=[
                positioned_box("7.[单选题]", 20, 20, 180, 50),
                positioned_box("示例命题", 20, 80, 200, 110),
                positioned_box("A.", 20, 140, 70, 180),
                positioned_box("G", 620, 185, 660, 205),
                positioned_box("甲", 100, 210, 150, 250),
                positioned_box("B. 乙", 20, 300, 180, 340),
                positioned_box("正确答案:B", 20, 380, 220, 420),
            ],
            raw_output={"fixture": "left-stacked-unpaired-ui"},
        )
    )[0]

    assert "A" not in [option.label for option in question.options]


def test_left_stacked_split_option_requires_current_label_continuity() -> None:
    question = parse_ocr_page(
        OcrPageResult(
            page_width=720,
            page_height=1280,
            boxes=[
                positioned_box("8.[单选题]", 20, 20, 180, 50),
                positioned_box("示例命题", 20, 80, 200, 110),
                positioned_box("A. 甲", 20, 140, 180, 180),
                positioned_box("C.", 20, 220, 70, 260),
                positioned_box("丙", 100, 285, 150, 325),
                positioned_box("D. 丁", 20, 360, 180, 400),
                positioned_box("正确答案:A", 20, 440, 220, 480),
            ],
            raw_output={"fixture": "left-stacked-current-continuity"},
        )
    )[0]

    assert [(option.label, option.text) for option in question.options] == [
        ("A", "甲\nC.\n丙"),
        ("D", "丁"),
    ]


def test_empty_answer_markers_close_options_and_preserve_crop_boundary() -> None:
    for marker in ("正确答案", "正确答案:"):
        question = parse_ocr_page(
            page(
                "6.[单选题]",
                "题干",
                "A. 甲",
                "B. 乙",
                marker,
                "无法归属的文本",
                "C. 丙",
            )
        )[0]

        assert [(option.label, option.text) for option in question.options] == [
            ("A", "甲"),
            ("B", "乙"),
        ]
        assert question.correct_answer is None
        assert "MISSING_ANSWER" in anomaly_codes(question)
        assert "UNASSIGNED_TEXT" in anomaly_codes(question)
        assert question.source_y_end == 360


def test_same_row_split_metadata_values_are_consumed() -> None:
    question = parse_ocr_page(
        OcrPageResult(
            page_width=720,
            page_height=500,
            boxes=[
                positioned_box("1.[单选题]", 20, 20, 180, 50),
                positioned_box("示例命题", 20, 80, 200, 110),
                positioned_box("A. 甲", 20, 140, 150, 170),
                positioned_box("B. 乙", 20, 200, 150, 230),
                positioned_box("我的答案：", 20, 280, 180, 320),
                positioned_box("B", 170, 284, 200, 316),
                positioned_box("正确答案：", 20, 360, 180, 400),
                positioned_box("B", 185, 364, 205, 396),
            ],
            raw_output={"fixture": "split-metadata"},
        )
    )[0]

    assert question.source_correct_answer_raw == "B"
    assert question.correct_answer == "B"
    assert "UNASSIGNED_TEXT" not in anomaly_codes(question)
    assert "MISSING_ANSWER" not in anomaly_codes(question)
    assert question.raw_ocr_text.splitlines()[-4:] == [
        "我的答案：",
        "B",
        "正确答案：",
        "B",
    ]
    assert question.normalized_ocr_text.splitlines()[-4:] == [
        "我的答案:",
        "B",
        "正确答案:",
        "B",
    ]
    assert question.source_y_end == 280


@pytest.mark.parametrize(
    ("token", "left", "top", "right", "bottom"),
    [
        ("B", 20, 340, 40, 370),
        ("B", 320, 284, 340, 316),
        ("查看", 185, 284, 240, 316),
    ],
)
def test_empty_answer_marker_does_not_consume_unrelated_box(
    token: str, left: int, top: int, right: int, bottom: int
) -> None:
    question = parse_ocr_page(
        OcrPageResult(
            page_width=720,
            page_height=500,
            boxes=[
                positioned_box("2.[单选题]", 20, 20, 180, 50),
                positioned_box("示例命题", 20, 80, 200, 110),
                positioned_box("A. 甲", 20, 140, 150, 170),
                positioned_box("B. 乙", 20, 200, 150, 230),
                positioned_box("正确答案：", 20, 280, 180, 320),
                positioned_box(token, left, top, right, bottom),
            ],
            raw_output={"fixture": "unrelated-after-marker"},
        )
    )[0]

    assert question.source_correct_answer_raw is None
    assert question.correct_answer is None
    assert "UNASSIGNED_TEXT" in anomaly_codes(question)
    assert "MISSING_ANSWER" in anomaly_codes(question)
    assert question.source_y_end == 280


def test_split_correct_answers_preserve_conflict_detection() -> None:
    question = parse_ocr_page(
        OcrPageResult(
            page_width=720,
            page_height=500,
            boxes=[
                positioned_box("3.[单选题]", 20, 20, 180, 50),
                positioned_box("示例命题", 20, 80, 200, 110),
                positioned_box("A. 甲", 20, 140, 150, 170),
                positioned_box("B. 乙", 20, 200, 150, 230),
                positioned_box("正确答案：", 20, 280, 180, 320),
                positioned_box("A", 185, 284, 205, 316),
                positioned_box("正确答案：", 20, 360, 180, 400),
                positioned_box("B", 185, 364, 205, 396),
            ],
            raw_output={"fixture": "split-conflict"},
        )
    )[0]

    assert question.source_correct_answer_raw == "B"
    assert question.correct_answer is None
    assert "CONFLICTING_ANSWERS" in anomaly_codes(question)
    assert "UNASSIGNED_TEXT" not in anomaly_codes(question)


def test_blank_ocr_boxes_do_not_change_options_or_create_unassigned_text() -> None:
    question = parse_ocr_page(
        page(
            "7.[单选题]",
            "题干",
            "A. 甲",
            "   ",
            "B. 乙",
            "正确答案:A",
            "\t",
        )
    )[0]

    assert [(option.label, option.text) for option in question.options] == [
        ("A", "甲"),
        ("B", "乙"),
    ]
    assert "UNASSIGNED_TEXT" not in anomaly_codes(question)


def test_crop_boundary_uses_earliest_safety_marker_across_marker_types() -> None:
    question = parse_ocr_page(
        OcrPageResult(
            page_width=720,
            page_height=160,
            boxes=[
                box("1.[单选题]", 10),
                box("题干", 20),
                box("A. 甲", 30),
                box("B. 乙", 40),
                box("正确答案:A", 60),
                box("我的答案:A", 85),
            ],
            raw_output={"fixture": "marker-order"},
        )
    )[0]

    assert question.correct_answer == "A"
    assert question.source_y_end == 60
