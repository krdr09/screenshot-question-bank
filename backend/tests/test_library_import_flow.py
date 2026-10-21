import hashlib
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.ocr.mock import MockOcrEngine
from app.testing.synthetic_screenshots import generate_parsing_screenshot_set


def test_synthetic_ready_question_can_be_managed_rerun_deleted_and_batch_removed(tmp_path: Path) -> None:
    page = generate_parsing_screenshot_set(tmp_path / "fixtures")[0]
    engine = MockOcrEngine(
        default=page.ocr_result,
        results_by_sha256={hashlib.sha256(page.image_path.read_bytes()).hexdigest(): page.ocr_result},
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
                "category_path": "阶段7/题库",
                "batch_name": "管理流程",
                "source_relative_path": "合成课程/阶段7/题库/管理流程",
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
            if progress["status"] == "ready":
                break
            assert time.monotonic() < deadline, progress
            time.sleep(0.02)

        listed = client.get("/api/questions", params={"batch_id": batch["id"], "search": "第五项"})
        listed.raise_for_status()
        question = listed.json()["items"][0]
        saved = client.patch(
            f"/api/questions/{question['id']}",
            json={"stem": "人工题库题干", "correct_answer": "E", "display_mode": "text_first"},
        )
        saved.raise_for_status()
        rerun = client.post(f"/api/questions/{question['id']}/rerun-ocr", json={"confirmed": True})
        rerun.raise_for_status()
        removed = client.request("DELETE", f"/api/questions/{question['id']}", json={"confirmed": True})
        removed.raise_for_status()
        after_remove = client.get("/api/questions", params={"batch_id": batch["id"]}).json()
        batch_removed = client.request("DELETE", f"/api/batches/{batch['id']}", json={"confirmed": True})
        batch_removed.raise_for_status()

    assert listed.json()["total"] == 1
    assert question["category_path"] == "阶段7/题库"
    assert saved.json()["stem"] == "人工题库题干"
    assert rerun.json()["status"] == "ready"
    assert removed.json()["deleted"] is True and after_remove["total"] == 0
    assert batch_removed.json()["deleted_questions"] == 1
