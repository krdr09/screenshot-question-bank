from pathlib import Path

from PIL import Image, ImageDraw

from app.config import Settings
from app.ocr.base import OcrBox, OcrPageResult, Point
from app.services.relationships.ordering import resolve_order
from app.services.relationships.overlap import OverlapScore, score_directional_overlap


def box(text: str, y: int) -> OcrBox:
    return OcrBox(
        text=text,
        polygon=[
            Point(x=20, y=y),
            Point(x=500, y=y),
            Point(x=500, y=y + 35),
            Point(x=20, y=y + 35),
        ],
        confidence=0.99,
    )


def page(*entries: tuple[str, int]) -> OcrPageResult:
    return OcrPageResult(
        page_width=600,
        page_height=1000,
        boxes=[box(text, y) for text, y in entries],
        raw_output=None,
    )


def scrolling_pair(tmp_path: Path) -> tuple[Path, Path]:
    first = Image.new("RGB", (600, 1000), "white")
    second = Image.new("RGB", (600, 1000), "white")
    pattern = Image.new("RGB", (600, 300), "white")
    draw = ImageDraw.Draw(pattern)
    for y in range(0, 300, 30):
        draw.rectangle((20 + y, y, 300 + y, y + 12), fill=(20 + y // 3, 90, 130))
    first.paste(pattern, (0, 700))
    second.paste(pattern, (0, 0))
    first_path = tmp_path / "unreliable-z.png"
    second_path = tmp_path / "unreliable-a.png"
    first.save(first_path)
    second.save(second_path)
    return first_path, second_path


def solid_pair(tmp_path: Path) -> tuple[Path, Path]:
    first = tmp_path / "black.png"
    second = tmp_path / "white.png"
    Image.new("RGB", (600, 1000), "black").save(first)
    Image.new("RGB", (600, 1000), "white").save(second)
    return first, second


def test_combined_status_bar_text_does_not_create_overlap_candidate(
    tmp_path: Path,
) -> None:
    first_path, second_path = solid_pair(tmp_path)
    settings = Settings(_env_file=None)

    for status_text in (
        "09:41 80%",
        "中国移动 5G 09:41 80%",
        "📶 Wi-Fi 🔋 80%",
    ):
        result = score_directional_overlap(
            page((status_text, 900)),
            page((status_text, 10)),
            first_path,
            second_path,
            settings,
        )
        assert result.text_score == 0
        assert result.combined_score < settings.overlap_candidate_threshold


def test_status_like_tokens_inside_question_text_are_preserved(
    tmp_path: Path,
) -> None:
    first_path, second_path = solid_pair(tmp_path)
    settings = Settings(_env_file=None)
    text = "某电池剩余 80% 时还能工作多久"

    result = score_directional_overlap(
        page((text, 900)),
        page((text, 10)),
        first_path,
        second_path,
        settings,
    )

    assert result.text_score == 1


def test_directional_overlap_combines_normalized_text_and_visual_strips(
    tmp_path: Path,
) -> None:
    first_path, second_path = scrolling_pair(tmp_path)
    first = page(
        ("09:41  80%", 5),
        ("题干前半部分", 650),
        ("继续内容，Ａ选项", 760),
        ("最后一行", 850),
    )
    second = page(
        ("继续内容,A选项", 40),
        ("最后一行", 120),
        ("正确答案：A", 600),
    )
    settings = Settings(_env_file=None)

    forward = score_directional_overlap(
        first, second, first_path, second_path, settings
    )
    reverse = score_directional_overlap(
        second, first, second_path, first_path, settings
    )

    assert forward.text_score >= 0.95
    assert forward.visual_score >= 0.95
    assert forward.combined_score >= settings.overlap_auto_threshold
    assert forward.matched_text == ["继续内容,a选项", "最后一行"]
    assert reverse.combined_score < forward.combined_score


def score(source: str, target: str, value: float) -> OverlapScore:
    return OverlapScore(
        source_id=source,
        target_id=target,
        text_score=value,
        visual_score=value,
        combined_score=value,
        matched_text=[],
        overlap_ratio=0.3,
    )


def test_unique_high_confidence_chain_is_resolved_without_using_names() -> None:
    settings = Settings(_env_file=None)
    result = resolve_order(
        ["middle", "last", "first"],
        [score("first", "middle", 0.98), score("middle", "last", 0.97)],
        settings,
    )

    assert result.ordered_ids == ["first", "middle", "last"]
    assert result.auto_applied is True
    assert result.issues == []


def test_branch_near_tie_and_cycle_are_reported_instead_of_guessed() -> None:
    settings = Settings(_env_file=None)
    branch = resolve_order(
        ["a", "b", "c"],
        [score("a", "b", 0.93), score("a", "c", 0.91)],
        settings,
    )
    cycle = resolve_order(
        ["a", "b"],
        [score("a", "b", 0.99), score("b", "a", 0.99)],
        settings,
    )

    assert branch.auto_applied is False
    assert {issue.code for issue in branch.issues} >= {"BRANCH", "NEAR_TIE"}
    assert cycle.auto_applied is False
    assert {issue.code for issue in cycle.issues} == {"CYCLE"}
