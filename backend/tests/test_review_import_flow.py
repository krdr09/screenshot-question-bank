import hashlib
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.ocr.mock import MockOcrEngine
from app.testing.synthetic_screenshots import generate_parsing_screenshot_set


def test_synthetic_anomaly_import_can_be_edited_and_confirmed(tmp_path: Path) -> None:
    page = generate_parsing_screenshot_set(tmp_path / "fixtures")[2]
    engine = MockOcrEngine(
        results_by_sha256={
            hashlib.sha256(page.image_path.read_bytes()).hexdigest(): page.ocr_result
        }
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=True,
        frontend_dist=tmp_path / "missing",
        _env_file=None,
    )
    app = create_app(settings, engine)

    with TestClient(app) as client:
        batch = client.post(
            "/api/import-jobs",
            json={
                "course_name": "合成课程",
                "category_path": "阶段6/异常题",
                "batch_name": "人工检查流程",
                "source_relative_path": "合成课程/阶段6/人工检查流程",
            },
        ).json()
        client.post(
            f"/api/import-jobs/{batch['id']}/files",
            files=[
                ("files", (page.image_path.name, page.image_path.read_bytes(), "image/png")),
                ("relative_paths", (None, page.image_path.name)),
            ],
        ).raise_for_status()
        client.post(f"/api/import-jobs/{batch['id']}/process").raise_for_status()
        deadline = time.monotonic() + 4
        while True:
            progress = client.get(f"/api/import-jobs/{batch['id']}").json()
            if progress["status"] == "question_review":
                break
            assert time.monotonic() < deadline, progress
            time.sleep(0.02)

        review = client.get("/api/review-items", params={"batch_id": batch["id"]}).json()
        assert len(review["items"]) == 1
        candidate = review["items"][0]
        assert {flag["code"] for flag in candidate["anomaly_flags"]} >= {"MISSING_ANSWER"}
        saved = client.patch(
            f"/api/review-items/{candidate['id']}",
            json={
                "edited_ocr_text": "人工确认答案为 B",
                "correct_answer": "B",
                "display_mode": "text_first",
            },
        )
        saved.raise_for_status()
        confirmed = client.post(f"/api/review-items/{candidate['id']}/confirm")
        confirmed.raise_for_status()
        final_progress = client.get(f"/api/import-jobs/{batch['id']}").json()

    assert confirmed.json()["status"] == "ready"
    assert confirmed.json()["correct_answer"] == "B"
    assert final_progress["status"] == "ready"
    assert final_progress["high_confidence_count"] == 1
    assert final_progress["review_count"] == 0
