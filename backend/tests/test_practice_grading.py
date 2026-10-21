from pathlib import Path
from datetime import timedelta

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError

from app.config import Settings
from app.main import create_app
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
from app.ocr.mock import MockOcrEngine
from app.models.imports import utc_now
from app.services.practice.grading import (
    PracticeGradingError,
    normalize_selected_answer,
    save_answer,
    submit_session,
)
from app.services.practice.sessions import get_practice_session


def _question(batch, category, image, stem, question_type, answer, options, **extra):
    return QuestionCandidate(
        batch_id=batch.id,
        category_id=category.id,
        source_image=image,
        source_display_number=stem,
        question_type=question_type,
        stem=stem,
        options_json=options,
        correct_answer_json=answer,
        explanation=f"{stem}解析",
        raw_ocr_text=stem,
        normalized_ocr_text=stem,
        ocr_confidence=1,
        parse_confidence=1,
        display_mode="text",
        anomaly_flags_json=[],
        status="ready",
        **extra,
    )


def _app_and_seed(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing",
        _env_file=None,
    )
    app = create_app(settings, MockOcrEngine())
    with TestClient(app):
        with app.state.database.session_factory() as session:
            course = Course(name="判分课程")
            category = Category(course=course, path="判分章节")
            batch = ImportBatch(
                course=course,
                category=category,
                name="判分批次",
                source_relative_path="判分课程/判分章节/判分批次",
                status="ready",
            )
            session.add(batch)
            session.flush()
            image = SourceImage(
                batch=batch,
                source_relative_path="source.png",
                original_name="source.png",
                safe_name="source.png",
                relative_path="imports/source.png",
                sha256="8" * 64,
                mime_type="image/png",
                width=100,
                height=100,
                file_size=1,
                order_index=0,
                ocr_status="completed",
            )
            session.add(image)
            session.flush()
            single = _question(
                batch,
                category,
                image,
                "A-E 单选题",
                "single_choice",
                "E",
                [
                    {"label": letter, "text": f"选项{letter}"}
                    for letter in ["A", "B", "C", "D", "E"]
                ],
            )
            boolean = _question(
                batch,
                category,
                image,
                "反向判断题",
                "true_false",
                True,
                [{"label": "A", "text": "错误"}, {"label": "B", "text": "正确"}],
                source_correct_answer_raw="B",
                true_label="B",
                false_label="A",
            )
            unanswered = _question(
                batch,
                category,
                image,
                "留空题",
                "single_choice",
                "A",
                [{"label": "A", "text": "甲"}, {"label": "B", "text": "乙"}],
            )
            session.add_all([single, boolean, unanswered])
            session.commit()
            return app, [single.id, boolean.id, unanswered.id]


def _create(client: TestClient, ids: list[str], feedback_mode: str):
    response = client.post(
        "/api/practice-sessions",
        json={
            "selection": {"manual_question_ids": ids},
            "order_mode": "sequential",
            "feedback_mode": feedback_mode,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _answer(client: TestClient, session_id: str, item_id: str, value):
    return client.post(
        f"/api/practice-sessions/{session_id}/answers",
        json={"practice_item_id": item_id, "selected_answer": value},
    )


def test_immediate_grading_locks_first_answer_and_tracks_unanswered_separately(
    tmp_path: Path,
) -> None:
    app, ids = _app_and_seed(tmp_path)
    single_id, boolean_id, _ = ids
    with TestClient(app) as client:
        practice = _create(client, ids, "immediate")
        items = {item["question_id"]: item for item in practice["items"]}

        wrong = _answer(client, practice["id"], items[single_id]["id"], "B")
        assert wrong.status_code == 200, wrong.text
        assert wrong.json()["is_correct"] is False
        assert wrong.json()["correct_answer"] == "E"
        assert wrong.json()["explanation"] == "A-E 单选题解析"
        assert wrong.json()["locked"] is True

        same_retry = _answer(client, practice["id"], items[single_id]["id"], "B")
        changed_retry = _answer(client, practice["id"], items[single_id]["id"], "E")
        boolean_string = _answer(
            client, practice["id"], items[boolean_id]["id"], "true"
        )
        correct_boolean = _answer(
            client, practice["id"], items[boolean_id]["id"], True
        )
        before_submit = client.get(f"/api/practice-sessions/{practice['id']}/result")
        submitted = client.post(f"/api/practice-sessions/{practice['id']}/submit")
        submitted_again = client.post(f"/api/practice-sessions/{practice['id']}/submit")
        wrong_list = client.get("/api/wrong-questions")

        correct_later = _create(client, [single_id], "immediate")
        later_item = correct_later["items"][0]
        _answer(client, correct_later["id"], later_item["id"], "E")
        client.post(f"/api/practice-sessions/{correct_later['id']}/submit")
        wrong_after_correct = client.get("/api/wrong-questions")

    assert same_retry.status_code == 200
    assert changed_retry.status_code == 409
    assert boolean_string.status_code == 422
    assert correct_boolean.status_code == 200
    assert correct_boolean.json()["is_correct"] is True
    assert before_submit.status_code == 409
    assert submitted.status_code == submitted_again.status_code == 200
    summary = submitted.json()
    assert summary["total_count"] == 3
    assert summary["correct_count"] == 1
    assert summary["incorrect_count"] == 1
    assert summary["unanswered_count"] == 1
    assert summary["accuracy"] == 33.33
    assert summary["correct_count"] + summary["incorrect_count"] + summary["unanswered_count"] == summary["total_count"]
    assert wrong_list.json()["items"][0]["question_id"] == single_id
    assert wrong_list.json()["items"][0]["error_count"] == 1
    assert wrong_after_correct.json()["items"][0]["question_id"] == single_id
    assert wrong_after_correct.json()["items"][0]["error_count"] == 1


def test_deferred_answers_can_change_without_feedback_until_atomic_submit(
    tmp_path: Path,
) -> None:
    app, ids = _app_and_seed(tmp_path)
    with TestClient(app) as client:
        practice = _create(client, [ids[0]], "deferred")
        item = practice["items"][0]
        first = _answer(client, practice["id"], item["id"], "B")
        changed = _answer(client, practice["id"], item["id"], "E")
        active = client.get(f"/api/practice-sessions/{practice['id']}")
        submitted = client.post(f"/api/practice-sessions/{practice['id']}/submit")
        locked = _answer(client, practice["id"], item["id"], "B")

    assert first.status_code == changed.status_code == active.status_code == 200
    assert changed.json()["selected_answer"] == "E"
    assert changed.json()["locked"] is False
    for response in [first, changed, active]:
        assert "correct_answer" not in response.text
        assert "is_correct" not in response.text
        assert "解析" not in response.text
    assert submitted.status_code == 200
    assert submitted.json()["correct_count"] == 1
    assert submitted.json()["items"][0]["correct_answer"] == "E"
    assert locked.status_code == 409


def test_normalize_selected_answer_rejects_non_boolean_true_false_snapshot() -> None:
    class Item:
        question_snapshot_json = {
            "question_type": "true_false",
            "options": [{"label": "A"}, {"label": "B"}],
        }

    try:
        normalize_selected_answer(Item(), "true")
    except ValueError as error:
        assert "判断题答案" in str(error)
    else:
        raise AssertionError("字符串 true 不得被接受为 canonical boolean")


def test_stale_concurrent_answer_and_submit_requests_are_idempotent(
    tmp_path: Path,
) -> None:
    app, ids = _app_and_seed(tmp_path)
    with TestClient(app) as client:
        practice = _create(client, [ids[0]], "immediate")
        session_id = practice["id"]
        item_id = practice["items"][0]["id"]

        with (
            app.state.database.session_factory() as winner,
            app.state.database.session_factory() as stale,
        ):
            stale_practice = get_practice_session(stale, session_id)
            assert stale_practice.status == "active"
            stale_item = stale.get(PracticeItem, item_id)
            assert stale_item is not None and stale_item.attempt is None

            first = save_answer(winner, session_id, item_id, "B")
            retried = save_answer(stale, session_id, item_id, "B")

        assert first["locked"] is True
        assert retried == first

        second_practice = _create(client, [ids[1]], "deferred")
        second_session_id = second_practice["id"]
        with (
            app.state.database.session_factory() as winner,
            app.state.database.session_factory() as stale,
        ):
            stale_practice = get_practice_session(stale, second_session_id)
            assert stale_practice.status == "active"
            assert all(item.attempt is None for item in stale_practice.items)

            first_result = submit_session(winner, second_session_id)
            retried_result = submit_session(stale, second_session_id)

        assert retried_result == first_result
        with app.state.database.session_factory() as session:
            assert session.scalar(select(func.count(Attempt.id))) == 2


def test_stale_immediate_answer_conflict_and_transaction_failures_roll_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, ids = _app_and_seed(tmp_path)
    with TestClient(app) as client:
        practice = _create(client, [ids[0]], "immediate")
        session_id = practice["id"]
        item_id = practice["items"][0]["id"]
        with (
            app.state.database.session_factory() as winner,
            app.state.database.session_factory() as stale,
        ):
            stale_item = stale.get(PracticeItem, item_id)
            assert stale_item is not None and stale_item.attempt is None
            save_answer(winner, session_id, item_id, "B")
            with pytest.raises(PracticeGradingError) as conflict:
                save_answer(stale, session_id, item_id, "E")
        assert conflict.value.status_code == 409

        rollback_practice = _create(client, [ids[1]], "immediate")
        rollback_session_id = rollback_practice["id"]
        rollback_item_id = rollback_practice["items"][0]["id"]
        with app.state.database.session_factory() as failing:
            monkeypatch.setattr(
                failing,
                "commit",
                lambda: (_ for _ in ()).throw(RuntimeError("commit failed")),
            )
            with pytest.raises(RuntimeError, match="commit failed"):
                save_answer(failing, rollback_session_id, rollback_item_id, False)

        with app.state.database.session_factory() as session:
            assert session.scalar(
                select(Attempt).where(Attempt.session_id == rollback_session_id)
            ) is None
            assert session.scalar(
                select(WrongBookEntry).where(WrongBookEntry.question_id == ids[1])
            ) is None

        deferred = _create(client, [ids[2]], "deferred")
        deferred_session_id = deferred["id"]
        deferred_item_id = deferred["items"][0]["id"]
        _answer(client, deferred_session_id, deferred_item_id, "B").raise_for_status()
        with app.state.database.session_factory() as failing:
            monkeypatch.setattr(
                failing,
                "commit",
                lambda: (_ for _ in ()).throw(RuntimeError("submit failed")),
            )
            with pytest.raises(RuntimeError, match="submit failed"):
                submit_session(failing, deferred_session_id)

        with app.state.database.session_factory() as session:
            stored = session.get(PracticeSession, deferred_session_id)
            assert stored is not None and stored.status == "active"
            attempt = session.scalar(
                select(Attempt).where(Attempt.session_id == deferred_session_id)
            )
            assert attempt is not None
            assert attempt.is_correct is None
            assert attempt.locked_at is None
            assert session.scalar(
                select(WrongBookEntry).where(WrongBookEntry.question_id == ids[2])
            ) is None


def test_deleted_question_in_old_immediate_practice_keeps_attempt_without_wrong_book_entry(
    tmp_path: Path,
) -> None:
    app, ids = _app_and_seed(tmp_path)
    question_id = ids[0]
    with TestClient(app) as client:
        practice = _create(client, [question_id], "immediate")
        item = practice["items"][0]
        deleted = client.request(
            "DELETE", f"/api/questions/{question_id}", json={"confirmed": True}
        )
        answered = _answer(client, practice["id"], item["id"], "B")

        assert deleted.status_code == 200, deleted.text
        assert answered.status_code == 200, answered.text
        assert answered.json()["locked"] is True
        assert answered.json()["is_correct"] is False
        with app.state.database.session_factory() as session:
            attempt = session.scalar(
                select(Attempt).where(Attempt.session_id == practice["id"])
            )
            assert attempt is not None
            assert attempt.is_correct is False
            assert attempt.locked_at is not None
            assert session.scalar(
                select(WrongBookEntry).where(WrongBookEntry.question_id == question_id)
            ) is None


def test_deleted_question_in_old_deferred_practice_keeps_history_without_wrong_book_entry(
    tmp_path: Path,
) -> None:
    app, ids = _app_and_seed(tmp_path)
    question_id = ids[0]
    with TestClient(app) as client:
        practice = _create(client, [question_id], "deferred")
        item = practice["items"][0]
        saved = _answer(client, practice["id"], item["id"], "B")
        deleted = client.request(
            "DELETE", f"/api/questions/{question_id}", json={"confirmed": True}
        )
        submitted = client.post(f"/api/practice-sessions/{practice['id']}/submit")

        assert saved.status_code == 200, saved.text
        assert deleted.status_code == 200, deleted.text
        assert submitted.status_code == 200, submitted.text
        assert submitted.json()["incorrect_count"] == 1
        with app.state.database.session_factory() as session:
            attempt = session.scalar(
                select(Attempt).where(Attempt.session_id == practice["id"])
            )
            assert attempt is not None
            assert attempt.is_correct is False
            assert attempt.locked_at is not None
            assert session.scalar(
                select(WrongBookEntry).where(WrongBookEntry.question_id == question_id)
            ) is None


@pytest.mark.parametrize(
    ("status", "creates_wrong_book_entry"),
    [
        ("ready", True),
        ("review", False),
        ("ignored", False),
        ("deleted", False),
    ],
)
def test_wrong_book_writes_require_exact_ready_candidate_status(
    tmp_path: Path, status: str, creates_wrong_book_entry: bool
) -> None:
    app, ids = _app_and_seed(tmp_path)
    question_id = ids[0]
    with TestClient(app) as client:
        practice = _create(client, [question_id], "immediate")
        item = practice["items"][0]
        with app.state.database.session_factory() as session:
            candidate = session.get(QuestionCandidate, question_id)
            assert candidate is not None
            candidate.status = status
            session.commit()

        answered = _answer(client, practice["id"], item["id"], "B")
        assert answered.status_code == 200, answered.text
        with app.state.database.session_factory() as session:
            attempt = session.scalar(
                select(Attempt).where(Attempt.session_id == practice["id"])
            )
            assert attempt is not None
            assert attempt.is_correct is False
            assert attempt.locked_at is not None
            entry = session.scalar(
                select(WrongBookEntry).where(WrongBookEntry.question_id == question_id)
            )
            assert (entry is not None) is creates_wrong_book_entry


def test_later_wrong_answer_reactivates_mastered_ready_wrong_book_entry(
    tmp_path: Path,
) -> None:
    app, ids = _app_and_seed(tmp_path)
    question_id = ids[0]
    stale_timestamp = (utc_now() - timedelta(days=1)).replace(tzinfo=None)
    with TestClient(app) as client:
        first = _create(client, [question_id], "immediate")
        _answer(client, first["id"], first["items"][0]["id"], "B").raise_for_status()
        with app.state.database.session_factory() as session:
            entry = session.scalar(
                select(WrongBookEntry).where(WrongBookEntry.question_id == question_id)
            )
            assert entry is not None
            entry.mastered_at = stale_timestamp
            entry.updated_at = stale_timestamp
            session.commit()

        later = _create(client, [question_id], "immediate")
        _answer(client, later["id"], later["items"][0]["id"], "B").raise_for_status()
        with app.state.database.session_factory() as session:
            entry = session.scalar(
                select(WrongBookEntry).where(WrongBookEntry.question_id == question_id)
            )
            assert entry is not None
            assert entry.mastered_at is None
            assert entry.updated_at > stale_timestamp


def test_undoing_a_deletion_to_ready_restores_wrong_book_eligibility(
    tmp_path: Path,
) -> None:
    app, ids = _app_and_seed(tmp_path)
    question_id = ids[0]
    with TestClient(app) as client:
        practice = _create(client, [question_id], "immediate")
        deleted = client.request(
            "DELETE", f"/api/questions/{question_id}", json={"confirmed": True}
        )
        undone = client.post(f"/api/review-items/{question_id}/undo")
        answered = _answer(client, practice["id"], practice["items"][0]["id"], "B")

        assert deleted.status_code == 200, deleted.text
        assert undone.status_code == 200, undone.text
        assert answered.status_code == 200, answered.text
        with app.state.database.session_factory() as session:
            candidate = session.get(QuestionCandidate, question_id)
            assert candidate is not None and candidate.status == "ready"
            assert session.scalar(
                select(WrongBookEntry).where(WrongBookEntry.question_id == question_id)
            ) is not None


@pytest.mark.parametrize(
    ("feedback_mode", "error_type", "expected_wrong_book_write_attempts"),
    [
        ("immediate", OperationalError, 1),
        ("deferred", OperationalError, 1),
        ("immediate", IntegrityError, 2),
        ("deferred", IntegrityError, 2),
    ],
)
def test_wrong_book_execute_failure_rolls_back_grading_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    feedback_mode: str,
    error_type: type[Exception],
    expected_wrong_book_write_attempts: int,
) -> None:
    app, ids = _app_and_seed(tmp_path)
    question_id = ids[0]
    with TestClient(app) as client:
        practice = _create(client, [question_id], feedback_mode)
        item = practice["items"][0]
        if feedback_mode == "deferred":
            _answer(client, practice["id"], item["id"], "B").raise_for_status()

        with app.state.database.session_factory() as session:
            original_execute = session.execute
            wrong_book_write_attempts = 0

            def fail_wrong_book_write(statement, *args, **kwargs):
                nonlocal wrong_book_write_attempts
                if (
                    getattr(getattr(statement, "table", None), "name", None)
                    == WrongBookEntry.__tablename__
                ):
                    wrong_book_write_attempts += 1
                    raise error_type("wrong-book execute failed", {}, RuntimeError())
                return original_execute(statement, *args, **kwargs)

            monkeypatch.setattr(session, "execute", fail_wrong_book_write)
            with pytest.raises(error_type, match="wrong-book execute failed"):
                if feedback_mode == "immediate":
                    save_answer(session, practice["id"], item["id"], "B")
                else:
                    submit_session(session, practice["id"])

            assert wrong_book_write_attempts == expected_wrong_book_write_attempts
            assert not session.in_transaction()
            assert not session.new
            if feedback_mode == "immediate":
                assert session.scalar(
                    select(Attempt).where(Attempt.session_id == practice["id"])
                ) is None
            else:
                attempt = session.scalar(
                    select(Attempt).where(Attempt.session_id == practice["id"])
                )
                assert attempt is not None
                assert attempt.is_correct is None
                assert attempt.locked_at is None
                stored = session.get(PracticeSession, practice["id"])
                assert stored is not None and stored.status == "active"
