from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.main import create_app
from app.models import (
    CandidateImagePart,
    Category,
    Course,
    ImportBatch,
    PracticeItem,
    QuestionCandidate,
    SourceImage,
)
from app.ocr.mock import MockOcrEngine


def _assert_no_feedback(value: object) -> None:
    if isinstance(value, dict):
        assert {"correct_answer", "is_correct", "explanation"}.isdisjoint(value)
        for nested in value.values():
            _assert_no_feedback(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_no_feedback(nested)


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
            course = Course(name="会话课程")
            category = Category(course=course, path="会话章节")
            batch = ImportBatch(
                course=course,
                category=category,
                name="会话批次",
                source_relative_path="会话课程/会话章节/会话批次",
                status="ready",
            )
            session.add(batch)
            session.flush()
            source = SourceImage(
                batch=batch,
                source_relative_path="source.png",
                original_name="source.png",
                safe_name="source.png",
                relative_path="imports/source.png",
                sha256="9" * 64,
                mime_type="image/png",
                width=120,
                height=240,
                file_size=1,
                order_index=0,
                ocr_status="completed",
            )
            session.add(source)
            session.flush()
            first = QuestionCandidate(
                batch_id=batch.id,
                category_id=category.id,
                source_image=source,
                source_display_number="1",
                question_type="single_choice",
                stem="创建时题干",
                options_json=[
                    {"label": "A", "text": "甲"},
                    {"label": "B", "text": "乙"},
                ],
                correct_answer_json="B",
                explanation="创建时解析",
                raw_ocr_text="创建时题干",
                normalized_ocr_text="创建时题干",
                ocr_confidence=1,
                parse_confidence=1,
                display_mode="image_preferred",
                anomaly_flags_json=[],
                status="ready",
            )
            second = QuestionCandidate(
                batch_id=batch.id,
                category_id=category.id,
                source_image=source,
                source_display_number="2",
                question_type="true_false",
                stem="判断题题干",
                options_json=[
                    {"label": "A", "text": "错误"},
                    {"label": "B", "text": "正确"},
                ],
                correct_answer_json=True,
                source_correct_answer_raw="B",
                true_label="B",
                false_label="A",
                explanation="判断题解析",
                raw_ocr_text="判断题题干",
                normalized_ocr_text="判断题题干",
                ocr_confidence=1,
                parse_confidence=1,
                display_mode="text",
                anomaly_flags_json=[],
                status="ready",
            )
            session.add_all([first, second])
            session.flush()
            for name, color in [
                ("first-a.png", "red"),
                ("first-b.png", "blue"),
                ("second.png", "green"),
            ]:
                path = settings.data_dir / "crops" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (120, 80), color).save(path)
            session.add_all(
                [
                    CandidateImagePart(
                        candidate_id=first.id,
                        source_image_id=source.id,
                        crop_path="crops/first-a.png",
                        source_y_start=10,
                        source_y_end=90,
                        part_index=0,
                    ),
                    CandidateImagePart(
                        candidate_id=first.id,
                        source_image_id=source.id,
                        crop_path="crops/first-b.png",
                        source_y_start=90,
                        source_y_end=170,
                        part_index=1,
                    ),
                    CandidateImagePart(
                        candidate_id=second.id,
                        source_image_id=source.id,
                        crop_path="crops/second.png",
                        source_y_start=180,
                        source_y_end=230,
                        part_index=0,
                    ),
                ]
            )
            session.commit()
            return app, settings, batch.id, first.id, second.id


def _create(client: TestClient, batch_id: str, count: int = 20, order: str = "sequential"):
    return client.post(
        "/api/practice-sessions",
        json={
            "selection": {"batch_id": batch_id},
            "count": count,
            "order_mode": order,
            "feedback_mode": "deferred",
        },
    )


def test_create_session_freezes_questions_order_and_hides_feedback(tmp_path: Path) -> None:
    app, _, batch_id, first_id, _ = _app_and_seed(tmp_path)
    with TestClient(app) as client:
        created = _create(client, batch_id)
        assert created.status_code == 201, created.text
        payload = created.json()
        _assert_no_feedback(payload)
        assert payload["actual_count"] == 2
        assert payload["limit_adjusted"] is True
        assert "当前可用 2 题" in payload["notice"]
        assert [item["order_index"] for item in payload["items"]] == [0, 1]
        assert payload["items"][0]["stem"] == "创建时题干"
        assert [part["part_index"] for part in payload["items"][0]["image_parts"]] == [0, 1]
        session_id = payload["id"]

        with app.state.database.session_factory() as session:
            first = session.get(QuestionCandidate, first_id)
            first.stem = "题库后来修改的题干"
            first.correct_answer_json = "A"
            first.explanation = "题库后来修改的解析"
            session.commit()

        restored = client.get(f"/api/practice-sessions/{session_id}")
        by_item = client.get(
            f"/api/practice-sessions/{session_id}",
            params={"item_id": payload["items"][1]["id"]},
        )
        by_order = client.get(
            f"/api/practice-sessions/{session_id}", params={"order_index": 0}
        )
        missing_item = client.get(
            f"/api/practice-sessions/{session_id}", params={"item_id": "other-session-item"}
        )
        conflicting_locator = client.get(
            f"/api/practice-sessions/{session_id}",
            params={"item_id": payload["items"][0]["id"], "order_index": 0},
        )

    assert restored.status_code == 200
    restored_payload = restored.json()
    _assert_no_feedback(restored_payload)
    assert restored_payload["items"][0]["stem"] == "创建时题干"
    assert restored_payload["items"][0]["locked"] is False
    assert restored_payload["status"] == "active"
    assert by_item.status_code == by_order.status_code == 200
    assert [item["id"] for item in by_item.json()["items"]] == [payload["items"][1]["id"]]
    assert [item["order_index"] for item in by_order.json()["items"]] == [0]
    assert missing_item.status_code == 404
    assert conflicting_locator.status_code == 422


def test_session_crop_endpoint_is_owned_ordered_and_path_safe(tmp_path: Path) -> None:
    app, settings, batch_id, _, _ = _app_and_seed(tmp_path)
    with TestClient(app) as client:
        first_session = _create(client, batch_id, count=1).json()
        second_session = _create(client, batch_id, count=1).json()
        item = first_session["items"][0]
        first_crop = client.get(item["image_parts"][0]["url"])
        second_crop = client.get(item["image_parts"][1]["url"])
        cross_session = client.get(
            f"/api/practice-sessions/{second_session['id']}/items/{item['id']}/parts/0/content"
        )

        outside = tmp_path / "outside.png"
        Image.new("RGB", (10, 10), "black").save(outside)
        with app.state.database.session_factory() as session:
            stored = session.get(PracticeItem, item["id"])
            changed = list(stored.image_parts_snapshot_json)
            changed[0] = {**changed[0], "crop_path": "../outside.png"}
            stored.image_parts_snapshot_json = changed
            session.commit()
        unsafe = client.get(
            f"/api/practice-sessions/{first_session['id']}/items/{item['id']}/parts/0/content"
        )

    assert first_crop.status_code == second_crop.status_code == 200
    assert first_crop.content != second_crop.content
    assert cross_session.status_code == 404
    assert unsafe.status_code == 404
    assert str(settings.data_dir.resolve()) not in unsafe.text


@pytest.mark.parametrize(
    ("unsafe_crop_path", "resolved_relative_path"),
    [
        ("imports/source.png", "imports/source.png"),
        ("config/not-a-crop.txt", "config/not-a-crop.txt"),
        ("crops\\first-a.png", "crops/first-a.png"),
        ("crops//first-a.png", "crops/first-a.png"),
        ("crops/./first-a.png", "crops/first-a.png"),
        ("crops/../config/not-a-crop.txt", "config/not-a-crop.txt"),
        (123, "123"),
    ],
    ids=[
        "imports-root",
        "config-root",
        "backslash",
        "empty-segment",
        "dot-segment",
        "parent-segment",
        "non-string",
    ],
)
def test_session_crop_endpoint_rejects_every_path_outside_shared_authority(
    tmp_path: Path,
    unsafe_crop_path: object,
    resolved_relative_path: str,
) -> None:
    app, settings, batch_id, _, _ = _app_and_seed(tmp_path)
    decoy_bytes = b"practice-crop-path-must-not-serve-this"
    resolved_target = settings.data_dir / resolved_relative_path
    resolved_target.parent.mkdir(parents=True, exist_ok=True)
    resolved_target.write_bytes(decoy_bytes)

    with TestClient(app) as client:
        practice = _create(client, batch_id, count=1).json()
        item = practice["items"][0]
        with app.state.database.session_factory() as session:
            stored = session.get(PracticeItem, item["id"])
            changed = list(stored.image_parts_snapshot_json)
            changed[0] = {**changed[0], "crop_path": unsafe_crop_path}
            stored.image_parts_snapshot_json = changed
            session.commit()

        response = client.get(item["image_parts"][0]["url"])

    assert response.status_code == 404
    assert response.json() == {"detail": "题目裁切图片不存在"}
    assert decoy_bytes not in response.content
    assert str(settings.data_dir.resolve()) not in response.text


def test_session_crop_endpoint_rejects_invalid_unrequested_snapshot_part(
    tmp_path: Path,
) -> None:
    app, settings, batch_id, _, _ = _app_and_seed(tmp_path)
    decoy_bytes = b"unrequested-practice-part-must-still-be-validated"
    decoy = settings.data_dir / "config/not-a-crop.txt"
    decoy.parent.mkdir(parents=True, exist_ok=True)
    decoy.write_bytes(decoy_bytes)

    with TestClient(app) as client:
        practice = _create(client, batch_id, count=1).json()
        item = practice["items"][0]
        with app.state.database.session_factory() as session:
            stored = session.get(PracticeItem, item["id"])
            changed = list(stored.image_parts_snapshot_json)
            changed[1] = {**changed[1], "crop_path": "config/not-a-crop.txt"}
            stored.image_parts_snapshot_json = changed
            session.commit()

        response = client.get(item["image_parts"][0]["url"])

    assert response.status_code == 404
    assert response.json() == {"detail": "题目裁切图片不存在"}
    assert decoy_bytes not in response.content
    assert str(settings.data_dir.resolve()) not in response.text


def test_session_creation_rejects_empty_selection_and_random_order_is_persisted(
    tmp_path: Path,
) -> None:
    app, _, batch_id, _, _ = _app_and_seed(tmp_path)
    with TestClient(app) as client:
        empty = client.post(
            "/api/practice-sessions",
            json={
                "selection": {"manual_question_ids": ["missing"]},
                "order_mode": "sequential",
                "feedback_mode": "immediate",
            },
        )
        created = _create(client, batch_id, count=2, order="random")
        restored = client.get(f"/api/practice-sessions/{created.json()['id']}")

    assert empty.status_code == 422
    assert created.status_code == 201
    assert restored.status_code == 200
    assert [item["id"] for item in created.json()["items"]] == [
        item["id"] for item in restored.json()["items"]
    ]
