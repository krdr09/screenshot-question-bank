from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import pytest
from sqlalchemy import select

from app.config import Settings
from app.database import Database
from app.models import (
    Attempt,
    Category,
    Course,
    ImportBatch,
    PracticeItem,
    PracticeSession,
    QuestionCandidate,
    SourceImage,
    WrongBookEntry,
)
from app.services.practice.grading import save_answer
from app.services.review.editor import delete_candidate


WAIT_SECONDS = 5
BLOCKED_RESULT_SECONDS = 0.2


@dataclass(frozen=True)
class WrongBookRaceSeed:
    database: Database
    settings: Settings
    candidate_id: str
    practice_session_id: str
    practice_item_id: str


@pytest.fixture
def wrong_book_race_seed(tmp_path: Path) -> Iterator[WrongBookRaceSeed]:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing",
        _env_file=None,
    )
    database = Database(settings)
    database.create_schema()
    try:
        with database.session_factory() as session:
            course = Course(name="wrong-book race course")
            category = Category(course=course, path="wrong-book race category")
            batch = ImportBatch(
                course=course,
                category=category,
                name="wrong-book race batch",
                source_relative_path="wrong-book/race/batch",
                status="ready",
                found_question_count=1,
                high_confidence_count=1,
            )
            source = SourceImage(
                batch=batch,
                source_relative_path="race.png",
                original_name="race.png",
                safe_name="race.png",
                relative_path="imports/race.png",
                sha256="d" * 64,
                mime_type="image/png",
                width=100,
                height=100,
                file_size=1,
                order_index=0,
                ocr_status="completed",
            )
            session.add_all([batch, source])
            session.flush()
            candidate = QuestionCandidate(
                batch_id=batch.id,
                category_id=category.id,
                source_image=source,
                source_display_number="1",
                question_type="single_choice",
                stem="Which answer is correct?",
                options_json=[
                    {"label": "A", "text": "correct"},
                    {"label": "B", "text": "wrong"},
                ],
                correct_answer_json="A",
                explanation="A is correct.",
                raw_ocr_text="Which answer is correct?",
                normalized_ocr_text="Which answer is correct?",
                ocr_confidence=1,
                parse_confidence=1,
                display_mode="text",
                anomaly_flags_json=[],
                status="ready",
            )
            session.add(candidate)
            session.flush()
            practice = PracticeSession(
                feedback_mode="immediate",
                order_mode="sequential",
                selection_json={"manual_question_ids": [candidate.id]},
                requested_count=1,
                actual_count=1,
                limit_adjusted=False,
                status="active",
            )
            item = PracticeItem(
                session=practice,
                question_id=candidate.id,
                order_index=0,
                question_snapshot_json={
                    "question_id": candidate.id,
                    "question_type": "single_choice",
                    "stem": "Which answer is correct?",
                    "options": [
                        {"label": "A", "text": "correct"},
                        {"label": "B", "text": "wrong"},
                    ],
                    "display_mode": "text",
                    "source_display_number": "1",
                    "course_id": course.id,
                    "course_name": course.name,
                    "category_id": category.id,
                    "category_path": category.path,
                    "batch_id": batch.id,
                    "batch_name": batch.name,
                    "batch_position": 1,
                },
                answer_snapshot_json="A",
                explanation_snapshot="A is correct.",
                image_parts_snapshot_json=[],
            )
            session.add_all([practice, item])
            session.commit()
            seed = WrongBookRaceSeed(
                database=database,
                settings=settings,
                candidate_id=candidate.id,
                practice_session_id=practice.id,
                practice_item_id=item.id,
            )
        yield seed
    finally:
        database.dispose()


def _is_conditional_wrong_book_upsert(statement: object, database: Database) -> bool:
    statement_text = str(statement.compile(dialect=database.engine.dialect)).upper()
    return (
        "INSERT INTO WRONG_BOOK_ENTRIES" in statement_text
        and "FROM QUESTION_CANDIDATES" in statement_text
    )


def _is_wrong_book_delete(statement: object, database: Database) -> bool:
    statement_text = str(statement.compile(dialect=database.engine.dialect)).upper()
    return "DELETE FROM WRONG_BOOK_ENTRIES" in statement_text


def _assert_deleted_without_wrong_book_and_with_incorrect_attempt(
    seed: WrongBookRaceSeed,
) -> None:
    with seed.database.session_factory() as session:
        candidate = session.get(QuestionCandidate, seed.candidate_id)
        wrong_book_entry = session.scalar(
            select(WrongBookEntry).where(
                WrongBookEntry.question_id == seed.candidate_id
            )
        )
        attempt = session.scalar(
            select(Attempt).where(
                Attempt.practice_item_id == seed.practice_item_id
            )
        )

        assert candidate is not None and candidate.status == "deleted"
        assert wrong_book_entry is None
        assert attempt is not None
        assert attempt.selected_answer_json == "B"
        assert attempt.is_correct is False
        assert attempt.was_unanswered is False
        assert attempt.locked_at is not None


def test_grading_first_then_deletion_removes_the_committed_wrong_book_entry(
    wrong_book_race_seed: WrongBookRaceSeed,
) -> None:
    seed = wrong_book_race_seed
    grading_upsert_executed = Event()
    allow_grading_to_commit = Event()
    deletion_candidate_read = Event()
    deletion_wrong_book_delete_attempted = Event()

    def grade_wrong_answer() -> dict:
        with seed.database.session_factory() as session:
            real_execute = session.execute

            def execute_then_hold_transaction(statement, *args, **kwargs):
                result = real_execute(statement, *args, **kwargs)
                if _is_conditional_wrong_book_upsert(statement, seed.database):
                    grading_upsert_executed.set()
                    if not allow_grading_to_commit.wait(WAIT_SECONDS):
                        raise TimeoutError("grading commit barrier was not released")
                return result

            session.execute = execute_then_hold_transaction
            return save_answer(
                session,
                seed.practice_session_id,
                seed.practice_item_id,
                "B",
            )

    def delete_question() -> None:
        with seed.database.session_factory() as session:
            real_scalar = session.scalar
            real_execute = session.execute

            def scalar_then_observe_candidate_read(statement, *args, **kwargs):
                result = real_scalar(statement, *args, **kwargs)
                selects_candidate = any(
                    description.get("entity") is QuestionCandidate
                    for description in getattr(statement, "column_descriptions", ())
                )
                if selects_candidate:
                    deletion_candidate_read.set()
                return result

            def observe_wrong_book_delete_then_execute(statement, *args, **kwargs):
                if _is_wrong_book_delete(statement, seed.database):
                    deletion_wrong_book_delete_attempted.set()
                return real_execute(statement, *args, **kwargs)

            session.scalar = scalar_then_observe_candidate_read
            session.execute = observe_wrong_book_delete_then_execute
            delete_candidate(session, seed.candidate_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        grading_future = pool.submit(grade_wrong_answer)
        deletion_future = None
        try:
            assert grading_upsert_executed.wait(WAIT_SECONDS)
            deletion_future = pool.submit(delete_question)
            assert deletion_candidate_read.wait(WAIT_SECONDS)
            assert deletion_wrong_book_delete_attempted.wait(WAIT_SECONDS)
            with pytest.raises(FutureTimeoutError):
                deletion_future.result(timeout=BLOCKED_RESULT_SECONDS)
        finally:
            allow_grading_to_commit.set()

        grading_result = grading_future.result(timeout=WAIT_SECONDS)
        assert deletion_future is not None
        deletion_future.result(timeout=WAIT_SECONDS)

    assert grading_result["is_correct"] is False
    _assert_deleted_without_wrong_book_and_with_incorrect_attempt(seed)


def test_deletion_first_then_grading_skips_wrong_book_insert_after_commit(
    wrong_book_race_seed: WrongBookRaceSeed,
) -> None:
    seed = wrong_book_race_seed
    deletion_at_commit = Event()
    allow_deletion_to_commit = Event()
    grading_persistence_checkpoint = Event()
    grading_upsert_attempted = Event()
    grading_ready_pre_read = Event()
    allow_grading_after_ready_pre_read = Event()

    def delete_question() -> None:
        with seed.database.session_factory() as session:
            real_commit = session.commit

            def hold_after_delete_flushes() -> None:
                deletion_at_commit.set()
                if not allow_deletion_to_commit.wait(WAIT_SECONDS):
                    raise TimeoutError("deletion commit barrier was not released")
                real_commit()

            session.commit = hold_after_delete_flushes
            delete_candidate(session, seed.candidate_id)

    def grade_wrong_answer() -> dict:
        with seed.database.session_factory() as session:
            real_execute = session.execute
            real_get = session.get

            def observe_upsert_then_execute(statement, *args, **kwargs):
                if _is_conditional_wrong_book_upsert(statement, seed.database):
                    grading_upsert_attempted.set()
                    grading_persistence_checkpoint.set()
                return real_execute(statement, *args, **kwargs)

            def hold_after_ready_candidate_pre_read(entity, ident, *args, **kwargs):
                result = real_get(entity, ident, *args, **kwargs)
                if (
                    entity is QuestionCandidate
                    and ident == seed.candidate_id
                    and result is not None
                    and result.status == "ready"
                ):
                    grading_ready_pre_read.set()
                    grading_persistence_checkpoint.set()
                    if not allow_grading_after_ready_pre_read.wait(WAIT_SECONDS):
                        raise TimeoutError("ready pre-read barrier was not released")
                return result

            session.execute = observe_upsert_then_execute
            session.get = hold_after_ready_candidate_pre_read
            return save_answer(
                session,
                seed.practice_session_id,
                seed.practice_item_id,
                "B",
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        deletion_future = pool.submit(delete_question)
        grading_future = None
        try:
            assert deletion_at_commit.wait(WAIT_SECONDS)
            grading_future = pool.submit(grade_wrong_answer)
            assert grading_persistence_checkpoint.wait(WAIT_SECONDS)
            if grading_upsert_attempted.is_set():
                assert not grading_ready_pre_read.is_set()
                with pytest.raises(FutureTimeoutError):
                    grading_future.result(timeout=BLOCKED_RESULT_SECONDS)
            else:
                assert grading_ready_pre_read.is_set()
                allow_deletion_to_commit.set()
                deletion_future.result(timeout=WAIT_SECONDS)
                allow_grading_after_ready_pre_read.set()
        finally:
            allow_deletion_to_commit.set()
            allow_grading_after_ready_pre_read.set()

        deletion_future.result(timeout=WAIT_SECONDS)
        assert grading_future is not None
        grading_result = grading_future.result(timeout=WAIT_SECONDS)

    assert grading_result["is_correct"] is False
    _assert_deleted_without_wrong_book_and_with_incorrect_attempt(seed)
