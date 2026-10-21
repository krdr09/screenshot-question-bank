from __future__ import annotations

import hashlib
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.ocr.mock import MockOcrEngine
from app.testing.synthetic_screenshots import generate_parsing_screenshot_set


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _migrate(database_url: str, monkeypatch) -> None:
    monkeypatch.setenv("QUESTION_PRACTICE_DATABASE_URL", database_url)
    command.upgrade(Config(str(BACKEND_ROOT / "alembic.ini")), "head")


def _wait_until_processed(client: TestClient, batch_id: str) -> dict:
    deadline = time.monotonic() + 8
    while True:
        progress = client.get(f"/api/import-jobs/{batch_id}").json()
        if progress["processed_count"] == 3:
            return progress
        assert time.monotonic() < deadline, progress
        time.sleep(0.02)


def _managed_image_snapshot(data_dir: Path) -> dict[str, str]:
    return {
        path.relative_to(data_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for folder in ("imports", "crops")
        for path in sorted((data_dir / folder).rglob("*"))
        if path.is_file()
    }


def _import_and_confirm(
    client: TestClient,
    pages,
) -> tuple[str, list[dict]]:
    created = client.post(
        "/api/import-jobs",
        json={
            "course_name": "阶段九合成课程",
            "category_path": "Mock OCR/完整恢复",
            "batch_name": "备份恢复合成批次",
            "source_relative_path": "阶段九合成课程/Mock OCR/完整恢复",
        },
    )
    created.raise_for_status()
    batch_id = created.json()["id"]
    multipart = [
        ("files", (page.image_path.name, page.image_path.read_bytes(), "image/png"))
        for page in pages
    ] + [
        (
            "relative_paths",
            (None, f"阶段九合成课程/Mock OCR/{page.image_path.name}"),
        )
        for page in pages
    ]
    client.post(f"/api/import-jobs/{batch_id}/files", files=multipart).raise_for_status()
    client.post(f"/api/import-jobs/{batch_id}/process").raise_for_status()
    progress = _wait_until_processed(client, batch_id)
    assert progress["found_question_count"] == 3

    review = client.get("/api/review-items", params={"batch_id": batch_id}).json()
    anomaly = next(
        item
        for item in review["items"]
        if any(flag["code"] == "MISSING_ANSWER" for flag in item["anomaly_flags"])
    )
    patched = client.patch(
        f"/api/review-items/{anomaly['id']}",
        json={"correct_answer": "A", "display_mode": "text_first"},
    )
    patched.raise_for_status()
    client.post(f"/api/review-items/{anomaly['id']}/confirm").raise_for_status()

    ready = client.get(
        "/api/practice-candidates", params={"batch_id": batch_id}
    )
    ready.raise_for_status()
    assert ready.json()["total"] == 3
    return batch_id, ready.json()["items"]


def _answer_wrong_master_and_submit(
    client: TestClient,
    question: dict,
) -> tuple[str, str]:
    created = client.post(
        "/api/practice-sessions",
        json={
            "selection": {"manual_question_ids": [question["id"]]},
            "order_mode": "sequential",
            "feedback_mode": "immediate",
        },
    )
    created.raise_for_status()
    practice = created.json()
    item = practice["items"][0]
    answered = client.post(
        f"/api/practice-sessions/{practice['id']}/answers",
        json={"practice_item_id": item["id"], "selected_answer": "A"},
    )
    answered.raise_for_status()
    assert answered.json()["is_correct"] is False
    assert answered.json()["correct_answer"] == "E"
    client.post(f"/api/practice-sessions/{practice['id']}/submit").raise_for_status()
    mastered = client.post(f"/api/wrong-questions/{question['id']}/master")
    mastered.raise_for_status()
    assert mastered.json()["mastered"] is True
    return practice["id"], item["id"]


def test_synthetic_questions_images_attempts_and_wrong_book_survive_backup_restore(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pages = generate_parsing_screenshot_set(tmp_path / "fixtures")
    engine = MockOcrEngine(
        results_by_sha256={
            hashlib.sha256(page.image_path.read_bytes()).hexdigest(): page.ocr_result
            for page in pages
        }
    )
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    database_url = f"sqlite:///{(data_dir / 'app.db').as_posix()}"
    _migrate(database_url, monkeypatch)
    settings = Settings(
        data_dir=data_dir,
        database_url=database_url,
        auto_create_schema=False,
        start_background_worker=True,
        frontend_dist=tmp_path / "missing-dist",
        _env_file=None,
    )
    app = create_app(settings, engine)

    with TestClient(app, raise_server_exceptions=False) as client:
        batch_id, questions = _import_and_confirm(client, pages)
        choice_with_e = next(item for item in questions if "第五项" in item["stem"])
        question_ids = {item["id"] for item in questions}
        practice_id, practice_item_id = _answer_wrong_master_and_submit(
            client, choice_with_e
        )
        expected_images = _managed_image_snapshot(data_dir)
        submitted_practice = client.get(f"/api/practice-sessions/{practice_id}")
        submitted_practice.raise_for_status()
        submitted_crop_bytes = {
            part["url"]: client.get(part["url"]).content
            for item in submitted_practice.json()["items"]
            for part in item["image_parts"]
        }
        submitted_crop_urls = set(submitted_crop_bytes)
        assert submitted_crop_bytes
        assert len(expected_images) == 6

        created = client.post("/api/backups")
        assert created.status_code == 201, created.text
        backup = created.json()
        assert backup["counts"] == {
            "questions": 3,
            "source_images": 3,
            "crop_images": 3,
            "attempts": 1,
            "wrong_questions": 1,
        }

        deleted = client.request(
            "DELETE", f"/api/batches/{batch_id}", json={"confirmed": True}
        )
        deleted.raise_for_status()
        assert client.get("/api/practice-candidates").json()["total"] == 0
        assert set(_managed_image_snapshot(data_dir).values()) == {
            hashlib.sha256(content).hexdigest()
            for content in submitted_crop_bytes.values()
        }
        historical_practice = client.get(f"/api/practice-sessions/{practice_id}")
        historical_practice.raise_for_status()
        historical_crop_urls = {
            part["url"]
            for item in historical_practice.json()["items"]
            for part in item["image_parts"]
        }
        assert historical_crop_urls == submitted_crop_urls
        for url in historical_crop_urls:
            crop = client.get(url)
            assert crop.status_code == 200, (url, crop.text)
            assert crop.content == submitted_crop_bytes[url]

        validated = client.post(
            "/api/backups/validate", data={"backup_id": backup["backup_id"]}
        )
        assert validated.status_code == 200, validated.text
        assert validated.json()["hashes_verified"] is True
        assert validated.json()["counts"] == backup["counts"]
        restored = client.post(
            "/api/backups/restore",
            json={
                "validation_token": validated.json()["validation_token"],
                "confirmed": True,
            },
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["restart_required"] is True
        assert restored.json()["safety_backup"]["reason"] == "pre_restore"
        assert client.get("/api/practice-candidates").status_code == 503

    # Restore intentionally seals the old process. A real restart runs migrations
    # before opening the restored database, even when the archive is already at head.
    _migrate(database_url, monkeypatch)
    restarted_settings = settings.model_copy(update={"start_background_worker": False})
    restarted = create_app(restarted_settings, MockOcrEngine())
    with TestClient(restarted, raise_server_exceptions=False) as client:
        ready = client.get("/api/practice-candidates", params={"batch_id": batch_id})
        ready.raise_for_status()
        assert ready.json()["total"] == 3
        assert {item["id"] for item in ready.json()["items"]} == question_ids

        practice = client.get(f"/api/practice-sessions/{practice_id}")
        practice.raise_for_status()
        assert practice.json()["status"] == "submitted"
        restored_item = next(
            item for item in practice.json()["items"] if item["id"] == practice_item_id
        )
        assert restored_item["selected_answer"] == "A"
        assert restored_item["is_correct"] is False
        assert restored_item["correct_answer"] == "E"
        assert restored_item["locked"] is True

        wrong = client.get(
            "/api/wrong-questions", params={"include_mastered": True}
        )
        wrong.raise_for_status()
        restored_wrong = next(
            item
            for item in wrong.json()["items"]
            if item["question_id"] == choice_with_e["id"]
        )
        assert restored_wrong["error_count"] == 1
        assert restored_wrong["mastered"] is True
        assert _managed_image_snapshot(data_dir) == expected_images

        crop_urls = [
            part["url"]
            for item in practice.json()["items"]
            for part in item["image_parts"]
        ]
        assert crop_urls
        for url in crop_urls:
            image = client.get(url)
            assert image.status_code == 200, (url, image.text)
            assert image.headers["content-type"].startswith("image/")
