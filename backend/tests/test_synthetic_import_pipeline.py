import hashlib
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.ocr.mock import MockOcrEngine
from app.testing.synthetic_screenshots import generate_normal_screenshot_set


def test_three_synthetic_screenshots_complete_mock_ocr_pipeline(
    tmp_path: Path,
) -> None:
    pages = generate_normal_screenshot_set(tmp_path / "fixtures")
    results_by_sha256 = {
        hashlib.sha256(page.image_path.read_bytes()).hexdigest(): page.ocr_result
        for page in pages
    }
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=True,
        frontend_dist=tmp_path / "missing-dist",
        _env_file=None,
    )
    app = create_app(
        settings,
        ocr_engine=MockOcrEngine(results_by_sha256=results_by_sha256),
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/import-jobs",
            json={
                "course_name": "合成课程",
                "category_path": "正常样本",
                "batch_name": "三页",
                "source_relative_path": "合成课程/正常样本/三页",
            },
        ).json()
        multipart = []
        for page in pages:
            multipart.append(
                ("files", (page.image_path.name, page.image_path.read_bytes(), "image/png"))
            )
        for page in pages:
            multipart.append(
                (
                    "relative_paths",
                    (None, f"合成课程/正常样本/三页/{page.image_path.name}"),
                )
            )
        client.post(
            f"/api/import-jobs/{created['id']}/files", files=multipart
        ).raise_for_status()
        client.post(f"/api/import-jobs/{created['id']}/process").raise_for_status()

        deadline = time.monotonic() + 3
        while True:
            completed = client.get(f"/api/import-jobs/{created['id']}").json()
            if completed["status"] == "ordering_review":
                break
            assert time.monotonic() < deadline, completed
            time.sleep(0.02)

    assert completed["status"] == "ordering_review"
    assert completed["processed_count"] == 3
    assert [image["ocr_status"] for image in completed["images"]] == [
        "completed",
        "completed",
        "completed",
    ]
    assert [image["ocr_text"].splitlines()[0] for image in completed["images"]] == [
        "Question 1",
        "Question 2",
        "Question 3",
    ]
