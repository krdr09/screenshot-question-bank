from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import Attempt, PracticeItem, PracticeSession
from app.ocr.mock import MockOcrEngine


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing",
        _env_file=None,
    )


def _item(practice: PracticeSession, order: int, *, correct: bool | None) -> PracticeItem:
    item = PracticeItem(
        session=practice,
        order_index=order,
        question_snapshot_json={
            "question_id": f"deleted-question-{order}",
            "question_type": "single_choice",
            "stem": f"不得出现在历史摘要中的秘密题干 {order}",
            "options": [{"label": "A", "text": "秘密选项"}],
            "display_mode": "text",
            "source_display_number": str(order + 1),
            "course_id": "deleted-course",
            "course_name": "历史课程",
            "category_id": None,
            "category_path": "未分类",
            "batch_id": "deleted-batch",
            "batch_name": "历史批次",
            "batch_position": order + 1,
        },
        answer_snapshot_json="A",
        explanation_snapshot="不得泄露的秘密解析",
        image_parts_snapshot_json=[],
    )
    if correct is not None:
        item.attempt = Attempt(
            session=practice,
            selected_answer_json="B" if not correct else "A",
            is_correct=correct,
            was_unanswered=False,
        )
    return item


def test_practice_history_is_paginated_newest_first_and_never_leaks_answers(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), MockOcrEngine())
    with TestClient(app) as client:
        with app.state.database.session_factory() as session:
            older = PracticeSession(
                feedback_mode="immediate",
                order_mode="sequential",
                selection_json={},
                requested_count=2,
                actual_count=2,
                status="submitted",
                started_at=datetime(2026, 7, 17, 8, tzinfo=timezone.utc),
                submitted_at=datetime(2026, 7, 17, 8, 5, tzinfo=timezone.utc),
            )
            older.items = [_item(older, 0, correct=True), _item(older, 1, correct=False)]
            newer = PracticeSession(
                feedback_mode="deferred",
                order_mode="random",
                selection_json={},
                requested_count=1,
                actual_count=1,
                status="active",
                started_at=datetime(2026, 7, 18, 8, tzinfo=timezone.utc),
            )
            newer.items = [_item(newer, 0, correct=None)]
            session.add_all([older, newer])
            session.commit()
            older_id = older.id
            newer_id = newer.id

        first = client.get("/api/practice-sessions", params={"page": 1, "page_size": 1})
        second = client.get("/api/practice-sessions", params={"page": 2, "page_size": 1})

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == {
        "items": [
            {
                "id": newer_id,
                "feedback_mode": "deferred",
                "order_mode": "random",
                "status": "active",
                "actual_count": 1,
                "answered_count": 0,
                "correct_count": None,
                "incorrect_count": None,
                "unanswered_count": None,
                "started_at": "2026-07-18T08:00:00",
                "submitted_at": None,
            }
        ],
        "total": 2,
        "page": 1,
        "page_size": 1,
    }
    submitted = second.json()["items"][0]
    assert submitted == {
        "id": older_id,
        "feedback_mode": "immediate",
        "order_mode": "sequential",
        "status": "submitted",
        "actual_count": 2,
        "answered_count": 2,
        "correct_count": 1,
        "incorrect_count": 1,
        "unanswered_count": 0,
        "started_at": "2026-07-17T08:00:00",
        "submitted_at": "2026-07-17T08:05:00",
    }
    combined = first.text + second.text
    assert "correct_answer" not in combined
    assert "answer_snapshot" not in combined
    assert "selected_answer" not in combined
    assert "秘密题干" not in combined
    assert "秘密选项" not in combined
    assert "秘密解析" not in combined


def test_practice_history_empty_page_is_explicit(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), MockOcrEngine())
    with TestClient(app) as client:
        response = client.get("/api/practice-sessions")

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "page": 1, "page_size": 20}
