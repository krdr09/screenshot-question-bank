from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.database import Database
from app.models import (
    Category,
    Course,
    ImportBatch,
    QuestionCandidate,
    ReviewAction,
    SourceImage,
)
from app.services.review.editor import INACTIVE_CANDIDATE_STATUSES, confirm_candidate
from app.services.review.query import (
    ReviewFilters,
    get_review_item,
    list_batch_context_items,
    list_review_items,
)


def _database(tmp_path: Path) -> Database:
    database = Database(
        Settings(
            data_dir=tmp_path / "data",
            database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
            auto_create_schema=True,
            start_background_worker=False,
            frontend_dist=tmp_path / "missing",
            _env_file=None,
        )
    )
    database.create_schema()
    return database


def _seed(
    session,
) -> tuple[
    ImportBatch,
    QuestionCandidate,
    QuestionCandidate,
    QuestionCandidate,
]:
    course = Course(name="医学")
    category = Category(course=course, path="内科/呼吸")
    batch = ImportBatch(
        course=course,
        category=category,
        name="异常批次",
        source_relative_path="医学/内科/异常批次",
        status="question_review",
    )
    image = SourceImage(
        batch=batch,
        source_relative_path="one.png",
        original_name="one.png",
        safe_name="one.png",
        relative_path="imports/one.png",
        sha256="1" * 64,
        mime_type="image/png",
        width=100,
        height=200,
        file_size=1,
        order_index=0,
        ocr_status="completed",
    )
    session.add_all([batch, image])
    session.flush()
    missing = QuestionCandidate(
        batch_id=batch.id,
        source_image=image,
        question_type="single_choice",
        stem="缺少答案",
        options_json=[{"label": "A", "text": "甲"}, {"label": "B", "text": "乙"}],
        raw_ocr_text="缺少答案",
        normalized_ocr_text="缺少答案",
        ocr_confidence=0.93,
        parse_confidence=0.55,
        display_mode="text_first",
        anomaly_flags_json=[{"code": "MISSING_ANSWER", "severity": "error", "message": "未识别到正确答案"}],
        status="review",
    )
    low = QuestionCandidate(
        batch_id=batch.id,
        source_image=image,
        question_type="true_false",
        stem="低置信度",
        options_json=[{"label": "A", "text": "正确"}, {"label": "B", "text": "错误"}],
        correct_answer_json=True,
        true_label="A",
        false_label="B",
        raw_ocr_text="低置信度",
        normalized_ocr_text="低置信度",
        ocr_confidence=0.42,
        parse_confidence=0.8,
        display_mode="image_first",
        anomaly_flags_json=[{"code": "LOW_OCR_CONFIDENCE", "severity": "warning", "message": "OCR 置信度低"}],
        status="review",
    )
    ready = QuestionCandidate(
        batch_id=batch.id,
        source_image=image,
        question_type="single_choice",
        stem="已处理",
        options_json=[{"label": "A", "text": "甲"}, {"label": "B", "text": "乙"}],
        correct_answer_json="A",
        raw_ocr_text="已处理",
        normalized_ocr_text="已处理",
        ocr_confidence=0.99,
        parse_confidence=0.99,
        display_mode="text_first",
        anomaly_flags_json=[],
        status="ready",
    )
    session.add_all([missing, low, ready])
    session.commit()
    return batch, missing, low, ready


def test_review_query_defaults_to_pending_anomalies_and_supports_filters(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with database.session_factory() as session:
        batch, missing, _, ready = _seed(session)

        pending = list_review_items(session, ReviewFilters(batch_id=batch.id))
        errors = list_review_items(
            session,
            ReviewFilters(batch_id=batch.id, anomaly_code="MISSING_ANSWER", severity="error"),
        )
        true_false = list_review_items(
            session, ReviewFilters(batch_id=batch.id, question_type="true_false")
        )
        named = list_review_items(
            session,
            ReviewFilters(
                course_name="医学",
                category_path="内科/呼吸",
                batch_name="异常批次",
            ),
        )

    assert {item.candidate.id for item in pending} == {missing.id, *(item.candidate.id for item in true_false)}
    assert [item.candidate.id for item in errors] == [missing.id]
    assert len(true_false) == 1 and true_false[0].course_name == "医学"
    assert true_false[0].category_path == "内科/呼吸"
    assert {item.candidate.id for item in named} == {item.candidate.id for item in pending}


def test_processed_requires_an_active_review_action(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with database.session_factory() as session:
        batch, _, low, direct_ready = _seed(session)
        confirm_candidate(session, low.id)

        processed = list_review_items(
            session,
            ReviewFilters(batch_id=batch.id, handling_status="processed"),
        )

    assert [item.candidate.id for item in processed] == [low.id]
    assert direct_ready.id not in {item.candidate.id for item in processed}


def test_all_is_pending_plus_processed_but_direct_lookup_is_unrestricted(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with database.session_factory() as session:
        batch, missing, low, direct_ready = _seed(session)
        confirm_candidate(session, low.id)

        all_items = list_review_items(
            session,
            ReviewFilters(batch_id=batch.id, handling_status="all"),
        )
        direct_item = get_review_item(session, direct_ready.id)

    assert {item.candidate.id for item in all_items} == {missing.id, low.id}
    assert direct_item is not None
    assert direct_item.candidate.id == direct_ready.id


def test_undone_actions_do_not_make_ready_candidates_processed_or_in_all(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with database.session_factory() as session:
        batch, missing, low, direct_ready = _seed(session)
        session.add(
            ReviewAction(
                batch_id=batch.id,
                candidate_id=direct_ready.id,
                target_type="candidate",
                target_id=direct_ready.id,
                action_type="confirm",
                before_json={},
                after_json={},
                undone_at=datetime.now(timezone.utc),
            )
        )
        session.commit()

        processed = list_review_items(
            session,
            ReviewFilters(batch_id=batch.id, handling_status="processed"),
        )
        all_items = list_review_items(
            session,
            ReviewFilters(batch_id=batch.id, handling_status="all"),
        )

    assert processed == []
    assert {item.candidate.id for item in all_items} == {missing.id, low.id}


def test_batch_context_ignores_review_filters_and_includes_direct_ready(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with database.session_factory() as session:
        batch, first, hidden_middle, direct_ready = _seed(session)
        first.created_at = datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
        hidden_middle.created_at = datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc)
        direct_ready.created_at = datetime(2026, 1, 1, 0, 0, 3, tzinfo=timezone.utc)
        session.commit()

        filtered = list_review_items(
            session,
            ReviewFilters(
                batch_id=batch.id,
                anomaly_code="MISSING_ANSWER",
                severity="error",
            ),
        )
        context = list_batch_context_items(session, first.id)

    assert [item.candidate.id for item in filtered] == [first.id]
    assert context is not None
    assert [candidate.id for candidate in context] == [
        first.id,
        hidden_middle.id,
        direct_ready.id,
    ]


def test_batch_context_orders_source_images_before_inverted_candidate_timestamps(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with database.session_factory() as session:
        batch, last_by_source, first_by_source, middle_by_source = _seed(session)
        last_by_source.source_image.order_index = 2
        first_image = SourceImage(
            batch=batch,
            source_relative_path="first.png",
            original_name="first.png",
            safe_name="first.png",
            relative_path="imports/first.png",
            sha256="2" * 64,
            mime_type="image/png",
            width=100,
            height=200,
            file_size=1,
            order_index=0,
            ocr_status="completed",
        )
        middle_image = SourceImage(
            batch=batch,
            source_relative_path="middle.png",
            original_name="middle.png",
            safe_name="middle.png",
            relative_path="imports/middle.png",
            sha256="3" * 64,
            mime_type="image/png",
            width=100,
            height=200,
            file_size=1,
            order_index=1,
            ocr_status="completed",
        )
        first_by_source.source_image = first_image
        middle_by_source.source_image = middle_image
        last_by_source.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        middle_by_source.created_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
        first_by_source.created_at = datetime(2026, 1, 3, tzinfo=timezone.utc)
        session.commit()

        context = list_batch_context_items(session, last_by_source.id)

    assert context is not None
    assert [candidate.id for candidate in context] == [
        first_by_source.id,
        middle_by_source.id,
        last_by_source.id,
    ]


@pytest.mark.parametrize(
    "inactive_status",
    sorted(INACTIVE_CANDIDATE_STATUSES),
)
def test_batch_context_excludes_every_inactive_status(
    tmp_path: Path,
    inactive_status: str,
) -> None:
    database = _database(tmp_path)
    with database.session_factory() as session:
        _, first, inactive, direct_ready = _seed(session)
        first.created_at = datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
        inactive.created_at = datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc)
        direct_ready.created_at = datetime(2026, 1, 1, 0, 0, 3, tzinfo=timezone.utc)
        inactive.status = inactive_status
        session.commit()

        context = list_batch_context_items(session, first.id)

    assert context is not None
    assert [candidate.id for candidate in context] == [first.id, direct_ready.id]


def test_inactive_anchor_resolves_its_batch_but_is_not_returned(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with database.session_factory() as session:
        _, inactive_anchor, second, direct_ready = _seed(session)
        inactive_anchor.created_at = datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
        second.created_at = datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc)
        direct_ready.created_at = datetime(2026, 1, 1, 0, 0, 3, tzinfo=timezone.utc)
        inactive_anchor.status = "ignored"
        session.commit()

        context = list_batch_context_items(session, inactive_anchor.id)

    assert context is not None
    assert [candidate.id for candidate in context] == [second.id, direct_ready.id]


def test_batch_context_is_limited_to_anchor_batch_and_missing_anchor_is_none(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with database.session_factory() as session:
        batch, first, _, _ = _seed(session)
        other_batch = ImportBatch(
            course=batch.course,
            name="其他批次",
            source_relative_path="医学/内科/其他批次",
            status="question_review",
        )
        other_image = SourceImage(
            batch=other_batch,
            source_relative_path="other.png",
            original_name="other.png",
            safe_name="other.png",
            relative_path="imports/other.png",
            sha256="9" * 64,
            mime_type="image/png",
            width=100,
            height=200,
            file_size=1,
            order_index=0,
            ocr_status="completed",
        )
        session.add_all([other_batch, other_image])
        session.flush()
        other_candidate = QuestionCandidate(
            batch_id=other_batch.id,
            source_image=other_image,
            question_type="single_choice",
            stem="其他批次候选题",
            options_json=[{"label": "A", "text": "一"}, {"label": "B", "text": "二"}],
            correct_answer_json="A",
            raw_ocr_text="其他批次候选题",
            normalized_ocr_text="其他批次候选题",
            ocr_confidence=0.99,
            parse_confidence=0.99,
            display_mode="text_first",
            anomaly_flags_json=[],
            status="ready",
        )
        session.add(other_candidate)
        session.commit()

        context = list_batch_context_items(session, first.id)
        missing = list_batch_context_items(session, "missing-candidate")

    assert context is not None
    assert other_candidate.id not in {candidate.id for candidate in context}
    assert missing is None
