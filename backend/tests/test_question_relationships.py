from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app.config import Settings
from app.services.relationships.cross_screenshot import assess_cross_screenshot
from app.services.relationships.duplicates import assess_duplicate
from app.services.relationships.overlap import OverlapScore


def candidate_crop(path: Path, accent: str = "navy") -> None:
    image = Image.new("RGB", (320, 180), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 300, 50), fill=accent)
    draw.line((30, 90, 290, 90), fill="black", width=4)
    image.save(path)


def dhash_image(path: Path, bits: int) -> None:
    pixels: list[int] = []
    for row in range(8):
        value = 128
        pixels.append(value)
        for column in range(8):
            offset = 63 - (row * 8 + column)
            value += -1 if (bits >> offset) & 1 else 1
            pixels.append(value)
    image = Image.new("L", (9, 8))
    image.putdata(pixels)
    image.save(path)


def test_same_batch_duplicate_requires_text_image_and_matching_answer(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    candidate_crop(first)
    candidate_crop(second)

    assessment = assess_duplicate(
        "下列说法正确的是|A甲|B乙",
        "下列说法正确的是 | A甲 | B乙",
        first,
        second,
        "A",
        "A",
        same_batch=True,
        settings=Settings(_env_file=None),
    )

    assert assessment is not None
    assert assessment.relation_type == "same_batch_duplicate"
    assert assessment.decision == "auto_merged"
    assert assessment.text_score >= 0.97
    assert assessment.hash_distance == 0


def test_hash_distance_just_outside_auto_threshold_enters_review(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    dhash_image(first, 0)
    dhash_image(second, 0b1111111)

    assessment = assess_duplicate(
        "题干|A甲|B乙",
        "题干|A甲|B乙",
        first,
        second,
        "A",
        "A",
        True,
        Settings(_env_file=None),
    )

    assert assessment is not None
    assert assessment.hash_distance == 7
    assert assessment.decision == "review"


def test_hash_distance_outside_review_margin_is_ignored_for_near_text(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    dhash_image(first, 0)
    dhash_image(second, 0b111111111)

    assessment = assess_duplicate(
        "这是用于验证视觉门禁并保持高文本相似度的示例命题|A甲|B乙",
        "这是用于验证视觉门禁并保持高文本相似度的实例命题|A甲|B乙",
        first,
        second,
        "A",
        "A",
        True,
        Settings(_env_file=None),
    )

    assert assessment is None


def test_exact_same_batch_text_and_answer_enters_review_outside_visual_margin(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    dhash_image(first, 0)
    dhash_image(second, 0b111111111)

    assessment = assess_duplicate(
        "示例命题|A甲|B乙",
        "示例命题|A甲|B乙",
        first,
        second,
        "A",
        "A",
        True,
        Settings(_env_file=None),
    )

    assert assessment is not None
    assert assessment.relation_type == "same_batch_duplicate"
    assert assessment.decision == "review"
    assert assessment.text_score == 1.0
    assert assessment.hash_distance == 9
    assert assessment.evidence["exact_text_match"] is True
    assert assessment.evidence["visual_gate_bypassed_for_review"] is True


@pytest.mark.parametrize(
    ("first_text", "second_text", "first_answer", "second_answer", "same_batch"),
    [
        ("", "", "A", "A", True),
        ("示例命题|A甲|B乙", "示例命题|A甲|B乙", None, None, True),
        ("示例命题|A甲|B乙", "示例命题|A甲|B乙", "A", "B", True),
        ("示例命题|A甲|B乙", "示例命题|A甲|B乙", "A", "A", False),
    ],
)
def test_exact_text_visual_bypass_requires_nonempty_same_batch_matching_answers(
    tmp_path: Path,
    first_text: str,
    second_text: str,
    first_answer: str | None,
    second_answer: str | None,
    same_batch: bool,
) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    dhash_image(first, 0)
    dhash_image(second, 0b111111111)

    assert (
        assess_duplicate(
            first_text,
            second_text,
            first,
            second,
            first_answer,
            second_answer,
            same_batch,
            Settings(_env_file=None),
        )
        is None
    )


def test_answer_conflict_is_review_and_cross_batch_match_stays_independent(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    candidate_crop(first)
    candidate_crop(second)
    settings = Settings(_env_file=None)

    conflict = assess_duplicate(
        "题干|A甲|B乙", "题干|A甲|B乙", first, second, "A", "B", True, settings
    )
    cross_batch = assess_duplicate(
        "题干|A甲|B乙", "题干|A甲|B乙", first, second, "A", "A", False, settings
    )

    assert conflict is not None and conflict.decision == "review"
    assert conflict.evidence["answer_conflict"] is True
    assert cross_batch is not None
    assert cross_batch.relation_type == "cross_batch_match"
    assert cross_batch.decision == "kept_separate"


def test_cross_screenshot_truncation_only_creates_unconfirmed_suggestion() -> None:
    overlap = OverlapScore(
        source_id="image-a",
        target_id="image-b",
        text_score=0.9,
        visual_score=0.95,
        combined_score=0.92,
        matched_text=["题干继续"],
        overlap_ratio=0.3,
    )

    suggestion = assess_cross_screenshot(
        first_flags={"TRUNCATED_BOTTOM"},
        second_flags={"MISSING_TITLE", "TRUNCATED_TOP"},
        overlap=overlap,
        first_text="第1题 题干前半部分",
        second_text="题干继续 A.甲 正确答案:A",
        settings=Settings(_env_file=None),
        first_display_number="7",
        second_display_number="8",
        first_question_type="single_choice",
        second_question_type="true_false",
    )

    assert suggestion is not None
    assert suggestion.decision == "suggested"
    assert suggestion.requires_confirmation is True
    assert suggestion.continuation_mode == "boundary_flag"
    assert suggestion.merged_text_preview == "第1题 题干前半部分\n题干继续 A.甲 正确答案:A"


def test_cross_screenshot_without_boundary_evidence_is_not_suggested() -> None:
    overlap = OverlapScore("a", "b", 0.99, 0.99, 0.99, [], 0.3)
    assert (
        assess_cross_screenshot(
            set(), set(), overlap, "完整题目", "另一道完整题", Settings(_env_file=None)
        )
        is None
    )


def test_repeated_title_identity_is_cross_screenshot_continuation_evidence() -> None:
    overlap = OverlapScore(
        source_id="image-a",
        target_id="image-b",
        text_score=0.7,
        visual_score=0.8,
        combined_score=0.75,
        matched_text=["示例命题"],
        overlap_ratio=0.3,
    )

    suggestion = assess_cross_screenshot(
        first_flags={"TRUNCATED_BOTTOM"},
        second_flags=set(),
        overlap=overlap,
        first_text="7.[单选题]\n示例命题前半部分",
        second_text="7.[单选题]\n示例命题完整内容",
        settings=Settings(_env_file=None),
        first_display_number="7",
        second_display_number="7",
        first_question_type="single_choice",
        second_question_type="single_choice",
    )

    assert suggestion is not None
    assert suggestion.decision == "suggested"
    assert suggestion.requires_confirmation is True
    assert suggestion.continuation_mode == "repeated_title"
    assert suggestion.confidence == pytest.approx(0.8)


def test_incomplete_repeated_title_is_cross_screenshot_continuation_evidence() -> None:
    overlap = OverlapScore(
        source_id="image-a",
        target_id="image-b",
        text_score=0.7,
        visual_score=0.8,
        combined_score=0.75,
        matched_text=["示例命题"],
        overlap_ratio=0.3,
    )

    suggestion = assess_cross_screenshot(
        first_flags={"MISSING_ANSWER"},
        second_flags=set(),
        overlap=overlap,
        first_text="7.[单选题]\n示例命题不完整内容",
        second_text="7.[单选题]\n示例命题完整内容\n正确答案:A",
        settings=Settings(_env_file=None),
        first_display_number="7",
        second_display_number="7",
        first_question_type="single_choice",
        second_question_type="single_choice",
    )

    assert suggestion is not None
    assert suggestion.decision == "suggested"
    assert suggestion.requires_confirmation is True
    assert suggestion.continuation_mode == "repeated_title_incomplete"
    assert suggestion.confidence == pytest.approx(0.8)


@pytest.mark.parametrize(
    "first_flags",
    [
        {"MISSING_ANSWER"},
        {"MISSING_ANSWER", "LOW_OCR_CONFIDENCE"},
    ],
)
def test_incomplete_repeated_title_allows_secondary_low_confidence_advisory(
    first_flags: set[str],
) -> None:
    overlap = OverlapScore("image-a", "image-b", 0.75, 0.75, 0.75, [], 0.3)

    suggestion = assess_cross_screenshot(
        first_flags=first_flags,
        second_flags={"LOW_OCR_CONFIDENCE"},
        overlap=overlap,
        first_text="7.[单选题]\n不完整内容",
        second_text="7.[单选题]\n完整内容\n正确答案:A",
        settings=Settings(_env_file=None),
        first_display_number="7",
        second_display_number="7",
        first_question_type="single_choice",
        second_question_type="single_choice",
    )

    assert suggestion is not None
    assert suggestion.decision == "suggested"
    assert suggestion.requires_confirmation is True
    assert suggestion.continuation_mode == "repeated_title_incomplete"


def test_physical_truncation_precedes_incomplete_repeated_title_mode() -> None:
    overlap = OverlapScore("a", "b", 0.75, 0.75, 0.75, [], 0.3)

    suggestion = assess_cross_screenshot(
        first_flags={"TRUNCATED_BOTTOM", "MISSING_ANSWER"},
        second_flags=set(),
        overlap=overlap,
        first_text="前半部分",
        second_text="完整内容",
        settings=Settings(_env_file=None),
        first_display_number="7",
        second_display_number="7",
        first_question_type="single_choice",
        second_question_type="single_choice",
    )

    assert suggestion is not None
    assert suggestion.continuation_mode == "repeated_title"
    assert suggestion.confidence == pytest.approx(0.8)


@pytest.mark.parametrize(
    (
        "first_flags",
        "second_flags",
        "first_display",
        "second_display",
        "first_type",
        "second_type",
        "combined_score",
    ),
    [
        (set(), set(), "7", "7", "single_choice", "single_choice", 0.75),
        (
            {"MISSING_ANSWER"},
            {"LOW_OCR_CONFIDENCE", "MISSING_ANSWER"},
            "7",
            "7",
            "single_choice",
            "single_choice",
            0.75,
        ),
        (
            {"MISSING_ANSWER"},
            set(),
            None,
            "7",
            "single_choice",
            "single_choice",
            0.75,
        ),
        (
            {"MISSING_ANSWER"},
            set(),
            "7",
            "8",
            "single_choice",
            "single_choice",
            0.75,
        ),
        (
            {"MISSING_ANSWER"},
            set(),
            "7",
            "7",
            None,
            "single_choice",
            0.75,
        ),
        (
            {"MISSING_ANSWER"},
            set(),
            "7",
            "7",
            "single_choice",
            "true_false",
            0.75,
        ),
        (
            {"MISSING_ANSWER"},
            set(),
            "7",
            "7",
            "single_choice",
            "single_choice",
            0.54,
        ),
    ],
)
def test_incomplete_repeated_title_requires_every_strict_guard(
    first_flags: set[str],
    second_flags: set[str],
    first_display: str | None,
    second_display: str | None,
    first_type: str | None,
    second_type: str | None,
    combined_score: float,
) -> None:
    overlap = OverlapScore(
        "image-a",
        "image-b",
        combined_score,
        combined_score,
        combined_score,
        [],
        0.3,
    )

    assert (
        assess_cross_screenshot(
            first_flags,
            second_flags,
            overlap,
            "不完整内容",
            "完整内容",
            Settings(_env_file=None),
            first_display_number=first_display,
            second_display_number=second_display,
            first_question_type=first_type,
            second_question_type=second_type,
        )
        is None
    )


@pytest.mark.parametrize(
    (
        "first_display",
        "second_display",
        "first_type",
        "second_type",
        "first_flags",
        "combined_score",
    ),
    [
        (None, "7", "single_choice", "single_choice", {"TRUNCATED_BOTTOM"}, 0.75),
        ("", "", "single_choice", "single_choice", {"TRUNCATED_BOTTOM"}, 0.75),
        ("7", "8", "single_choice", "single_choice", {"TRUNCATED_BOTTOM"}, 0.75),
        ("7", "7", None, "single_choice", {"TRUNCATED_BOTTOM"}, 0.75),
        ("7", "7", "single_choice", "true_false", {"TRUNCATED_BOTTOM"}, 0.75),
        ("7", "7", "single_choice", "single_choice", set(), 0.75),
        ("7", "7", "single_choice", "single_choice", {"TRUNCATED_BOTTOM"}, 0.54),
    ],
)
def test_repeated_title_continuation_requires_all_strict_guards(
    first_display: str | None,
    second_display: str | None,
    first_type: str | None,
    second_type: str | None,
    first_flags: set[str],
    combined_score: float,
) -> None:
    overlap = OverlapScore(
        "image-a",
        "image-b",
        combined_score,
        combined_score,
        combined_score,
        [],
        0.3,
    )

    assert (
        assess_cross_screenshot(
            first_flags,
            set(),
            overlap,
            "前半部分",
            "完整内容",
            Settings(_env_file=None),
            first_display_number=first_display,
            second_display_number=second_display,
            first_question_type=first_type,
            second_question_type=second_type,
        )
        is None
    )
