from copy import deepcopy
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import select

from app.config import Settings
from app.database import Database
from app.models import (
    CandidateImagePart,
    CandidateRelationship,
    Course,
    ImportBatch,
    OrderingIssue,
    QuestionCandidate,
    ScreenshotOverlap,
    SourceImage,
)
from app.ocr.base import OcrBox, OcrPageResult, Point
from app.services.relationships.pipeline import analyze_batch_relationships


def ocr_box(text: str, y: int) -> OcrBox:
    return OcrBox(
        text=text,
        polygon=[Point(x=20, y=y), Point(x=500, y=y), Point(x=500, y=y + 30), Point(x=20, y=y + 30)],
        confidence=0.99,
    )


def make_pair(root: Path) -> tuple[Path, Path]:
    first = Image.new("RGB", (600, 1000), "white")
    second = Image.new("RGB", (600, 1000), "white")
    overlap = Image.new("RGB", (600, 300), "white")
    draw = ImageDraw.Draw(overlap)
    for y in range(0, 300, 25):
        draw.rectangle((20, y, 400, y + 10), fill=(30, 80 + y // 5, 120))
    first.paste(overlap, (0, 700))
    second.paste(overlap, (0, 0))
    first_path = root / "imports" / "first.png"
    second_path = root / "imports" / "second.png"
    first_path.parent.mkdir(parents=True)
    first.save(first_path)
    second.save(second_path)
    return first_path, second_path


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
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def candidate(batch: ImportBatch, image: SourceImage, stem: str, flags: list[str]) -> QuestionCandidate:
    return QuestionCandidate(
        batch_id=batch.id,
        source_image_id=image.id,
        question_type="single_choice",
        stem=stem,
        options_json=[{"label": "A", "text": "甲"}],
        correct_answer_json=None,
        raw_ocr_text=stem,
        normalized_ocr_text=stem,
        ocr_confidence=0.99,
        parse_confidence=0.7,
        display_mode="text",
        anomaly_flags_json=[{"code": code, "message": code} for code in flags],
        status="review",
    )


@pytest.mark.parametrize(
    (
        "continuation_mode",
        "first_flags",
        "second_flags",
        "display_number",
        "duplicate_kind",
    ),
    [
        pytest.param(
            "boundary_flag",
            ["TRUNCATED_BOTTOM"],
            ["MISSING_TITLE", "TRUNCATED_TOP"],
            None,
            None,
            id="boundary-flag",
        ),
        pytest.param(
            "repeated_title",
            ["TRUNCATED_BOTTOM"],
            [],
            "7",
            None,
            id="repeated-title",
        ),
        pytest.param(
            "repeated_title_incomplete",
            ["MISSING_ANSWER"],
            [],
            "7",
            None,
            id="incomplete-repeated-title",
        ),
        pytest.param(
            "repeated_title_incomplete",
            ["MISSING_ANSWER"],
            [],
            "7",
            "review",
            id="cross-precedes-duplicate-review",
        ),
        pytest.param(
            "repeated_title_incomplete",
            ["MISSING_ANSWER", "LOW_OCR_CONFIDENCE"],
            ["LOW_OCR_CONFIDENCE"],
            "7",
            "review",
            id="advisory-low-cross-precedes-duplicate-review",
        ),
        pytest.param(
            "repeated_title",
            ["TRUNCATED_BOTTOM"],
            [],
            "7",
            "auto",
            id="cross-precedes-auto-duplicate",
        ),
    ],
)
def test_pipeline_persists_auto_order_and_unconfirmed_cross_screenshot_suggestion(
    tmp_path: Path,
    continuation_mode: str,
    first_flags: list[str],
    second_flags: list[str],
    display_number: str | None,
    duplicate_kind: str | None,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        _env_file=None,
    )
    first_path, second_path = make_pair(settings.data_dir)
    database = Database(settings)
    database.create_schema()
    with database.session_factory() as session:
        course = Course(name="关系课程")
        session.add(course)
        session.flush()
        batch = ImportBatch(
            course_id=course.id,
            name="阶段5",
            source_relative_path="关系课程/阶段5",
            status="ordering_review",
            uploaded_count=2,
            processed_count=2,
        )
        session.add(batch)
        session.flush()
        second = SourceImage(
            batch_id=batch.id,
            source_relative_path="random-a.png",
            original_name="random-a.png",
            safe_name="second.png",
            relative_path=second_path.relative_to(settings.data_dir).as_posix(),
            sha256="2" * 64,
            mime_type="image/png",
            width=600,
            height=1000,
            file_size=second_path.stat().st_size,
            order_index=0,
            ocr_status="completed",
            ocr_result_json=OcrPageResult(
                page_width=600,
                page_height=1000,
                boxes=[ocr_box("题干继续", 30), ocr_box("最后一行", 100), ocr_box("A.甲", 300)],
                raw_output=None,
            ).model_dump(mode="json"),
            ocr_text="题干继续\n最后一行\nA.甲",
        )
        first = SourceImage(
            batch_id=batch.id,
            source_relative_path="random-z.png",
            original_name="random-z.png",
            safe_name="first.png",
            relative_path=first_path.relative_to(settings.data_dir).as_posix(),
            sha256="1" * 64,
            mime_type="image/png",
            width=600,
            height=1000,
            file_size=first_path.stat().st_size,
            order_index=1,
            ocr_status="completed",
            ocr_result_json=OcrPageResult(
                page_width=600,
                page_height=1000,
                boxes=[ocr_box("第1题 前半部分", 400), ocr_box("题干继续", 760), ocr_box("最后一行", 850)],
                raw_output=None,
            ).model_dump(mode="json"),
            ocr_text="第1题 前半部分\n题干继续\n最后一行",
        )
        session.add_all([second, first])
        session.flush()
        first_stem = "相同候选" if duplicate_kind else "第1题 前半部分"
        second_stem = "相同候选" if duplicate_kind else "题干继续 A.甲"
        first_candidate = candidate(batch, first, first_stem, first_flags)
        second_candidate = candidate(batch, second, second_stem, second_flags)
        second_has_only_advisory_flags = set(second_flags) <= {
            "LOW_OCR_CONFIDENCE"
        }
        if second_has_only_advisory_flags:
            second_candidate.options_json = [
                {"label": "A", "text": "甲"},
                {"label": "B", "text": "乙"},
            ]
            second_candidate.correct_answer_json = "A"
            if not second_flags:
                second_candidate.status = "ready"
        if duplicate_kind:
            first_candidate.options_json = [
                {"label": "A", "text": "甲"},
                {"label": "B", "text": "乙"},
            ]
        if duplicate_kind == "auto":
            first_candidate.correct_answer_json = "A"
        if display_number is not None:
            first_candidate.source_display_number = display_number
            second_candidate.source_display_number = display_number
        session.add_all([first_candidate, second_candidate])
        session.flush()
        first_part_path = first_path
        second_part_path = second_path
        if duplicate_kind:
            first_part_path = settings.data_dir / "crops" / "first.png"
            second_part_path = settings.data_dir / "crops" / "second.png"
            dhash_image(first_part_path, 0)
            dhash_image(second_part_path, 0)
        for item, image, path in [
            (first_candidate, first, first_part_path),
            (second_candidate, second, second_part_path),
        ]:
            session.add(CandidateImagePart(candidate_id=item.id, source_image_id=image.id, crop_path=path.relative_to(settings.data_dir).as_posix(), source_y_start=0, source_y_end=500, part_index=0))
        session.commit()
        batch_id = batch.id
        first_id = first.id
        second_id = second.id
        first_candidate_id = first_candidate.id
        second_candidate_id = second_candidate.id
        first_stem_before = first_candidate.stem
        second_stem_before = second_candidate.stem
        first_options_before = list(first_candidate.options_json)
        second_options_before = list(second_candidate.options_json)
        first_answer_before = first_candidate.correct_answer_json
        second_answer_before = second_candidate.correct_answer_json
        first_flags_before = deepcopy(first_candidate.anomaly_flags_json)
        second_flags_before = deepcopy(second_candidate.anomaly_flags_json)
        first_status_before = first_candidate.status
        second_status_before = second_candidate.status

    summary = analyze_batch_relationships(database, settings, batch_id)

    with database.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        images = list(session.scalars(select(SourceImage).where(SourceImage.batch_id == batch_id).order_by(SourceImage.order_index)))
        overlaps = list(session.scalars(select(ScreenshotOverlap)))
        issues = list(session.scalars(select(OrderingIssue)))
        relations = list(session.scalars(select(CandidateRelationship)))
        parts = list(
            session.scalars(
                select(CandidateImagePart).where(
                    CandidateImagePart.candidate_id.in_(
                        [first_candidate_id, second_candidate_id]
                    )
                )
            )
        )
        first_candidate_after = session.get(QuestionCandidate, first_candidate_id)
        second_candidate_after = session.get(QuestionCandidate, second_candidate_id)
        assert [image.id for image in images] == [first_id, second_id]
        assert overlaps and overlaps[0].source_image_id == first_id
        assert issues == []
        assert len(relations) == 1
        assert relations[0].relation_type == "cross_screenshot"
        assert relations[0].decision == "suggested"
        assert relations[0].status == "pending"
        assert relations[0].evidence_json["requires_confirmation"] is True
        assert relations[0].evidence_json["continuation_mode"] == continuation_mode
        assert relations[0].evidence_json["source_image_ids"] == [
            first_id,
            second_id,
        ]
        assert {
            part.candidate_id: (
                part.source_image_id,
                part.crop_path,
                part.source_y_start,
                part.source_y_end,
                part.part_index,
            )
            for part in parts
        } == {
            first_candidate_id: (
                first_id,
                first_part_path.relative_to(settings.data_dir).as_posix(),
                0,
                500,
                0,
            ),
            second_candidate_id: (
                second_id,
                second_part_path.relative_to(settings.data_dir).as_posix(),
                0,
                500,
                0,
            ),
        }
        assert first_candidate_after is not None
        assert second_candidate_after is not None
        assert first_candidate_after.stem == first_stem_before
        assert second_candidate_after.stem == second_stem_before
        assert first_candidate_after.options_json == first_options_before
        assert second_candidate_after.options_json == second_options_before
        assert first_candidate_after.correct_answer_json == first_answer_before
        assert second_candidate_after.correct_answer_json == second_answer_before
        assert first_candidate_after.anomaly_flags_json == first_flags_before
        assert second_candidate_after.anomaly_flags_json == second_flags_before
        assert first_candidate_after.status == first_status_before
        assert second_candidate_after.status == second_status_before
        assert first_candidate_after.duplicate_of_id is None
        assert second_candidate_after.duplicate_of_id is None
        assert batch is not None and batch.status == "question_review"
    assert summary.order_auto_applied is True
    assert summary.auto_duplicate_count == 0
    assert summary.duplicate_review_count == 0
    assert summary.cross_screenshot_suggestions == 1
    database.dispose()


def test_pipeline_suggests_exact_text_duplicate_outside_visual_margin(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        _env_file=None,
    )
    source_path = settings.data_dir / "imports" / "source.png"
    source_path.parent.mkdir(parents=True)
    Image.new("RGB", (600, 1000), "white").save(source_path)
    first_crop = settings.data_dir / "crops" / "first.png"
    second_crop = settings.data_dir / "crops" / "second.png"
    dhash_image(first_crop, 0)
    dhash_image(second_crop, 0b111111111)

    database = Database(settings)
    database.create_schema()
    with database.session_factory() as session:
        course = Course(name="关系课程")
        session.add(course)
        session.flush()
        batch = ImportBatch(
            course_id=course.id,
            name="精确题文复核",
            source_relative_path="关系课程/精确题文复核",
            status="ready",
            uploaded_count=1,
            processed_count=1,
        )
        session.add(batch)
        session.flush()
        image = SourceImage(
            batch_id=batch.id,
            source_relative_path="source.png",
            original_name="source.png",
            safe_name="source.png",
            relative_path=source_path.relative_to(settings.data_dir).as_posix(),
            sha256="1" * 64,
            mime_type="image/png",
            width=600,
            height=1000,
            file_size=source_path.stat().st_size,
            order_index=0,
            ocr_status="completed",
            ocr_result_json=OcrPageResult(
                page_width=600,
                page_height=1000,
                boxes=[ocr_box("示例命题", 100)],
                raw_output=None,
            ).model_dump(mode="json"),
            ocr_text="示例命题",
        )
        session.add(image)
        session.flush()
        first_candidate = candidate(batch, image, "示例命题", [])
        second_candidate = candidate(batch, image, "示例命题", [])
        for item in (first_candidate, second_candidate):
            item.options_json = [
                {"label": "A", "text": "甲"},
                {"label": "B", "text": "乙"},
            ]
            item.correct_answer_json = "A"
            item.status = "ready"
        session.add_all([first_candidate, second_candidate])
        session.flush()
        for item, crop_path in (
            (first_candidate, first_crop),
            (second_candidate, second_crop),
        ):
            session.add(
                CandidateImagePart(
                    candidate_id=item.id,
                    source_image_id=image.id,
                    crop_path=crop_path.relative_to(settings.data_dir).as_posix(),
                    source_y_start=0,
                    source_y_end=8,
                    part_index=0,
                )
            )
        session.commit()
        batch_id = batch.id

    summary = analyze_batch_relationships(database, settings, batch_id)

    with database.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        relations = list(
            session.scalars(
                select(CandidateRelationship).where(
                    CandidateRelationship.batch_id == batch_id
                )
            )
        )
        assert len(relations) == 1
        relationship = relations[0]
        secondary = session.get(
            QuestionCandidate, relationship.secondary_candidate_id
        )
        assert relationship.relation_type == "same_batch_duplicate"
        assert relationship.decision == "review"
        assert relationship.status == "pending"
        assert relationship.evidence_json["exact_text_match"] is True
        assert (
            relationship.evidence_json["visual_gate_bypassed_for_review"]
            is True
        )
        assert secondary is not None and secondary.status == "review"
        assert secondary.duplicate_of_id is None
        assert batch is not None and batch.status == "question_review"
    assert summary.auto_duplicate_count == 0
    assert summary.duplicate_review_count == 1
    database.dispose()
