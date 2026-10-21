import random
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import (
    Attempt,
    CandidateImagePart,
    Category,
    Course,
    ImportBatch,
    PracticeItem,
    PracticeSession,
    QuestionCandidate,
    SourceImage,
    WrongBookEntry,
)
from app.ocr.mock import MockOcrEngine
from app.services.practice.selection import (
    PracticeSelectionError,
    SelectionCriteria,
    list_practice_candidates,
    select_questions,
)


def _question(
    batch: ImportBatch,
    category: Category,
    image: SourceImage,
    stem: str,
    status: str = "ready",
) -> QuestionCandidate:
    return QuestionCandidate(
        batch_id=batch.id,
        category_id=category.id,
        source_image=image,
        source_display_number="1",
        question_type="single_choice",
        stem=stem,
        options_json=[{"label": "A", "text": "甲"}, {"label": "B", "text": "乙"}],
        correct_answer_json="A",
        explanation=f"{stem}解析",
        raw_ocr_text=stem,
        normalized_ocr_text=stem.lower(),
        ocr_confidence=1,
        parse_confidence=1,
        display_mode="text",
        anomaly_flags_json=[],
        status=status,
    )


def _seed(session):
    course = Course(name="医学")
    chapter = Category(course=course, path="内科/第一章")
    other_chapter = Category(course=course, path="内科/第二章")
    other_course = Course(name="法规")
    law_chapter = Category(course=other_course, path="第一章")
    batch = ImportBatch(
        course=course,
        category=chapter,
        name="批次一",
        source_relative_path="医学/第一章/批次一",
        status="ready",
    )
    batch_two = ImportBatch(
        course=course,
        category=other_chapter,
        name="批次二",
        source_relative_path="医学/第二章/批次二",
        status="ready",
    )
    law_batch = ImportBatch(
        course=other_course,
        category=law_chapter,
        name="法规批次",
        source_relative_path="法规/第一章/法规批次",
        status="ready",
    )
    session.add_all([batch, batch_two, law_batch])
    session.flush()

    early = SourceImage(
        batch=batch,
        source_relative_path="early.png",
        original_name="early.png",
        safe_name="early.png",
        relative_path="imports/early.png",
        sha256="1" * 64,
        mime_type="image/png",
        width=100,
        height=200,
        file_size=1,
        order_index=0,
        ocr_status="completed",
    )
    later = SourceImage(
        batch=batch,
        source_relative_path="later.png",
        original_name="later.png",
        safe_name="later.png",
        relative_path="imports/later.png",
        sha256="2" * 64,
        mime_type="image/png",
        width=100,
        height=200,
        file_size=1,
        order_index=1,
        ocr_status="completed",
    )
    second_image = SourceImage(
        batch=batch_two,
        source_relative_path="second.png",
        original_name="second.png",
        safe_name="second.png",
        relative_path="imports/second.png",
        sha256="3" * 64,
        mime_type="image/png",
        width=100,
        height=200,
        file_size=1,
        order_index=0,
        ocr_status="completed",
    )
    law_image = SourceImage(
        batch=law_batch,
        source_relative_path="law.png",
        original_name="law.png",
        safe_name="law.png",
        relative_path="imports/law.png",
        sha256="4" * 64,
        mime_type="image/png",
        width=100,
        height=200,
        file_size=1,
        order_index=0,
        ocr_status="completed",
    )
    session.add_all([early, later, second_image, law_image])
    session.flush()

    answered = _question(batch, chapter, later, "已作答题")
    unanswered = _question(batch, chapter, early, "仅未答历史题")
    review = _question(batch, chapter, early, "待检查题", status="review")
    second = _question(batch_two, other_chapter, second_image, "第二章题")
    law = _question(law_batch, law_chapter, law_image, "法规题")
    session.add_all([answered, unanswered, review, second, law])
    session.flush()
    session.add_all(
        [
            CandidateImagePart(
                candidate_id=answered.id,
                source_image_id=later.id,
                crop_path="crops/answered.png",
                source_y_start=10,
                source_y_end=80,
                part_index=0,
            ),
            CandidateImagePart(
                candidate_id=unanswered.id,
                source_image_id=early.id,
                crop_path="crops/unanswered.png",
                source_y_start=80,
                source_y_end=150,
                part_index=0,
            ),
        ]
    )
    history = PracticeSession(
        feedback_mode="deferred",
        order_mode="sequential",
        selection_json={},
        actual_count=2,
        limit_adjusted=False,
        status="submitted",
    )
    answered_item = PracticeItem(
        session=history,
        question_id=answered.id,
        order_index=0,
        question_snapshot_json={"question_type": "single_choice", "options": answered.options_json},
        answer_snapshot_json="A",
        image_parts_snapshot_json=[],
    )
    unanswered_item = PracticeItem(
        session=history,
        question_id=unanswered.id,
        order_index=1,
        question_snapshot_json={"question_type": "single_choice", "options": unanswered.options_json},
        answer_snapshot_json="A",
        image_parts_snapshot_json=[],
    )
    session.add_all(
        [
            history,
            answered_item,
            unanswered_item,
            Attempt(
                session=history,
                item=answered_item,
                selected_answer_json="B",
                is_correct=False,
                was_unanswered=False,
            ),
            Attempt(
                session=history,
                item=unanswered_item,
                selected_answer_json=None,
                is_correct=None,
                was_unanswered=True,
            ),
            WrongBookEntry(
                question_id=answered.id,
                mastered_at=datetime.now(timezone.utc),
            ),
            WrongBookEntry(question_id=unanswered.id),
        ]
    )
    session.commit()
    return {
        "course": course,
        "chapter": chapter,
        "batch": batch,
        "answered": answered,
        "unanswered": unanswered,
        "review": review,
        "second": second,
        "law": law,
    }


def _app(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing",
        _env_file=None,
    )
    return create_app(settings, MockOcrEngine())


def test_selection_filters_ready_wrong_and_unanswered_questions(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app):
        with app.state.database.session_factory() as session:
            seeded = _seed(session)
            course_items = list_practice_candidates(
                session, SelectionCriteria(course_id=seeded["course"].id)
            )
            chapter_items = list_practice_candidates(
                session,
                SelectionCriteria(
                    course_id=seeded["course"].id,
                    category_id=seeded["chapter"].id,
                    question_type="single_choice",
                ),
            )
            wrong_items = list_practice_candidates(
                session, SelectionCriteria(membership="wrong")
            )
            not_done = list_practice_candidates(
                session, SelectionCriteria(membership="unanswered")
            )

    assert [item.question.id for item in course_items] == [
        seeded["unanswered"].id,
        seeded["answered"].id,
        seeded["second"].id,
    ]
    assert [item.question.id for item in chapter_items] == [
        seeded["unanswered"].id,
        seeded["answered"].id,
    ]
    assert [item.question.id for item in wrong_items] == [seeded["unanswered"].id]
    assert seeded["answered"].id not in {item.question.id for item in not_done}
    assert {seeded["unanswered"].id, seeded["second"].id, seeded["law"].id} <= {
        item.question.id for item in not_done
    }
    assert seeded["review"].id not in {item.question.id for item in course_items}


def test_selection_uses_internal_position_manual_ids_counts_and_stable_random(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    with TestClient(app):
        with app.state.database.session_factory() as session:
            seeded = _seed(session)
            first_position = list_practice_candidates(
                session,
                SelectionCriteria(
                    batch_id=seeded["batch"].id,
                    position_start=1,
                    position_end=1,
                ),
            )
            manual, _ = select_questions(
                session,
                SelectionCriteria(
                    manual_question_ids=(seeded["law"].id, seeded["answered"].id)
                ),
                None,
                "sequential",
            )
            capped, adjusted = select_questions(
                session,
                SelectionCriteria(course_id=seeded["course"].id),
                20,
                "sequential",
            )
            random_one, _ = select_questions(
                session,
                SelectionCriteria(course_id=seeded["course"].id),
                None,
                "random",
                rng=random.Random(7),
            )
            random_two, _ = select_questions(
                session,
                SelectionCriteria(course_id=seeded["course"].id),
                None,
                "random",
                rng=random.Random(7),
            )

            with pytest.raises(PracticeSelectionError, match="正式题库"):
                select_questions(
                    session,
                    SelectionCriteria(manual_question_ids=("missing",)),
                    None,
                    "sequential",
                )
            with pytest.raises(PracticeSelectionError, match="不能同时"):
                list_practice_candidates(
                    session,
                    SelectionCriteria(
                        batch_id=seeded["batch"].id,
                        manual_question_ids=(seeded["answered"].id,),
                        position_start=1,
                        position_end=1,
                    ),
                )

    assert first_position[0].question.id == seeded["unanswered"].id
    assert first_position[0].batch_position == 1
    assert {item.question.id for item in manual} == {
        seeded["law"].id,
        seeded["answered"].id,
    }
    assert adjusted is True and len(capped) == 3
    assert [item.question.id for item in random_one] == [
        item.question.id for item in random_two
    ]


def test_selection_excludes_flagged_primary_and_flagged_part_sources(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    with TestClient(app):
        with app.state.database.session_factory() as session:
            seeded = _seed(session)
            seeded["unanswered"].source_image.review_flag = "unrelated"
            flagged_part_source = SourceImage(
                batch=seeded["batch"],
                source_relative_path="flagged-part.png",
                original_name="flagged-part.png",
                safe_name="flagged-part.png",
                relative_path="imports/flagged-part.png",
                sha256="9" * 64,
                mime_type="image/png",
                width=100,
                height=200,
                file_size=1,
                review_flag="damaged",
                order_index=2,
                ocr_status="completed",
            )
            session.add(flagged_part_source)
            session.flush()
            session.add(
                CandidateImagePart(
                    candidate_id=seeded["answered"].id,
                    source_image_id=flagged_part_source.id,
                    crop_path="crops/flagged-part.png",
                    source_y_start=0,
                    source_y_end=100,
                    part_index=1,
                )
            )
            session.commit()

            available = list_practice_candidates(
                session, SelectionCriteria(course_id=seeded["course"].id)
            )
            with pytest.raises(PracticeSelectionError, match="正式题库"):
                select_questions(
                    session,
                    SelectionCriteria(
                        manual_question_ids=(
                            seeded["unanswered"].id,
                            seeded["answered"].id,
                        )
                    ),
                    None,
                    "sequential",
                )

    assert [item.question.id for item in available] == [seeded["second"].id]
