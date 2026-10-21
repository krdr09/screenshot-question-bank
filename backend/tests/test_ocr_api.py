from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.main import create_app
from app.models import ImportBatch
from app.ocr.base import OcrBox, OcrEngine, OcrPageResult, Point
from app.ocr.mock import MockOcrEngine


def png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 48), (30, 60, 90)).save(output, format="PNG")
    return output.getvalue()


def mock_result() -> OcrPageResult:
    return OcrPageResult(
        page_width=32,
        page_height=48,
        boxes=[
            OcrBox(
                text="Question 1",
                polygon=[
                    Point(x=1, y=2),
                    Point(x=30, y=2),
                    Point(x=30, y=12),
                    Point(x=1, y=12),
                ],
                confidence=0.99,
            )
        ],
        raw_output={"fixture": 1},
    )


def make_client(
    tmp_path: Path, ocr_engine: OcrEngine | None = None
) -> TestClient:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing-dist",
        _env_file=None,
    )
    engine = (
        ocr_engine
        if ocr_engine is not None
        else MockOcrEngine(default=mock_result())
    )
    return TestClient(create_app(settings, ocr_engine=engine))


def create_uploaded_job(client: TestClient, batch_name: str = "作业1") -> dict:
    created = client.post(
        "/api/import-jobs",
        json={
            "course_name": "运筹学",
            "category_path": "第一章",
            "batch_name": batch_name,
            "source_relative_path": f"运筹学/第一章/{batch_name}",
        },
    ).json()
    response = client.post(
        f"/api/import-jobs/{created['id']}/files",
        files=[
            ("files", ("page.png", png_bytes(), "image/png")),
            ("relative_paths", (None, f"运筹学/第一章/{batch_name}/page.png")),
        ],
    )
    assert response.status_code == 200
    return created


def test_process_endpoint_queues_and_exposes_persisted_progress(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        job = create_uploaded_job(client)

        queued = client.post(f"/api/import-jobs/{job['id']}/process")
        assert queued.status_code == 202
        assert queued.json()["status"] == "queued"

        assert client.app.state.ocr_worker.run_next() == job["id"]
        progress = client.get(f"/api/import-jobs/{job['id']}")

    assert progress.status_code == 200
    body = progress.json()
    assert body["status"] == "question_review"
    assert body["processed_count"] == 1
    assert body["uploaded_count"] == 1
    assert body["ocr_device"] == "mock"
    assert body["current_file"] is None
    assert body["error_log"] == []
    assert body["images"][0]["ocr_status"] == "completed"


def test_import_job_api_replaces_internal_ocr_paths_with_public_message(
    tmp_path: Path,
) -> None:
    windows_path = r"C:\private\ocr models\detector.bin"
    posix_path = "/opt/private/ocr models/classifier.bin"
    internal_message = f"cannot open {windows_path}; fallback missing {posix_path}"

    class PathFailingEngine:
        device = "cpu"

        def recognize(self, image_path: Path) -> OcrPageResult:
            raise OSError(internal_message)

    with make_client(tmp_path, PathFailingEngine()) as client:
        job = create_uploaded_job(client)
        client.post(f"/api/import-jobs/{job['id']}/process").raise_for_status()
        assert client.app.state.ocr_worker.run_next() == job["id"]

        with client.app.state.database.session_factory() as session:
            batch = session.get(ImportBatch, job["id"])
            assert batch is not None
            assert batch.images[0].ocr_error == internal_message
            assert batch.error_log_json == [
                {"file": "page.png", "message": internal_message}
            ]

        progress = client.get(f"/api/import-jobs/{job['id']}")

    assert progress.status_code == 200
    body = progress.json()
    public_message = "OCR 处理失败，请查看本地日志"
    assert body["error_log"] == [
        {"file": "page.png", "message": public_message}
    ]
    assert body["images"][0]["ocr_error"] == public_message
    assert set(body["error_log"][0]) == {"file", "message"}
    assert windows_path not in progress.text
    assert posix_path not in progress.text


def test_process_endpoint_rejects_failed_job_without_mutation(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        job = create_uploaded_job(client)
        with client.app.state.database.session_factory() as session:
            batch = session.get(ImportBatch, job["id"])
            assert batch is not None
            image = batch.images[0]
            batch.status = "failed"
            batch.processed_count = 1
            batch.error_count = 1
            batch.error_log_json = [
                {"file": image.original_name, "message": "原始失败"}
            ]
            image.ocr_status = "failed"
            image.ocr_error = "原始失败"
            session.commit()

        before = client.get(f"/api/import-jobs/{job['id']}").json()
        rejected = client.post(f"/api/import-jobs/{job['id']}/process")
        after = client.get(f"/api/import-jobs/{job['id']}").json()

    assert rejected.status_code == 400
    assert rejected.json()["detail"] == "导入任务当前状态不允许处理：failed"
    assert after == before


def test_cancel_endpoint_only_marks_requested_job(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        first = create_uploaded_job(client, "批次1")
        second = create_uploaded_job(client, "批次2")
        client.post(f"/api/import-jobs/{first['id']}/process").raise_for_status()
        client.post(f"/api/import-jobs/{second['id']}/process").raise_for_status()

        cancelled = client.post(f"/api/import-jobs/{first['id']}/cancel")
        second_progress = client.get(f"/api/import-jobs/{second['id']}")

    assert cancelled.status_code == 202
    assert cancelled.json()["cancel_requested"] is True
    assert second_progress.json()["status"] == "queued"
    assert second_progress.json()["cancel_requested"] is False
