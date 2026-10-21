import hashlib
import time
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.main import create_app
from app.models import CandidateRelationship, ImportBatch, ScreenshotOverlap
from app.ocr.mock import MockOcrEngine
from app.testing.synthetic_screenshots import generate_relationship_screenshot_set


def create_and_process(client: TestClient, pages, batch_name: str) -> dict:
    created = client.post(
        "/api/import-jobs",
        json={
            "course_name": "阶段5合成课程",
            "category_path": "关系",
            "batch_name": batch_name,
            "source_relative_path": f"阶段5合成课程/关系/{batch_name}",
        },
    ).json()
    multipart = [
        ("files", (page.image_path.name, page.image_path.read_bytes(), "image/png"))
        for page in pages
    ] + [
        ("relative_paths", (None, f"{batch_name}/{page.image_path.name}"))
        for page in pages
    ]
    client.post(f"/api/import-jobs/{created['id']}/files", files=multipart).raise_for_status()
    client.post(f"/api/import-jobs/{created['id']}/process").raise_for_status()
    deadline = time.monotonic() + 5
    while True:
        progress = client.get(f"/api/import-jobs/{created['id']}").json()
        if progress["status"] in {"ordering_review", "question_review", "ready", "failed"}:
            return progress
        assert time.monotonic() < deadline, progress
        time.sleep(0.02)


def test_synthetic_import_runs_order_cross_screenshot_and_duplicate_pipeline(
    tmp_path: Path,
) -> None:
    pages = generate_relationship_screenshot_set(tmp_path / "fixtures")
    engine = MockOcrEngine(
        results_by_sha256={
            hashlib.sha256(page.image_path.read_bytes()).hexdigest(): page.ocr_result
            for page in pages
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
        cross = create_and_process(client, pages[:2], "跨截图")
        duplicate = create_and_process(client, pages[2:], "同批重复")

    with app.state.database.session_factory() as session:
        cross_batch = session.get(ImportBatch, cross["id"])
        duplicate_batch = session.get(ImportBatch, duplicate["id"])
        cross_relations = list(session.scalars(select(CandidateRelationship).where(CandidateRelationship.batch_id == cross["id"])))
        duplicate_relations = list(session.scalars(select(CandidateRelationship).where(CandidateRelationship.batch_id == duplicate["id"])))
        overlaps = list(session.scalars(select(ScreenshotOverlap).where(ScreenshotOverlap.batch_id == cross["id"])))
        assert cross_batch is not None and cross_batch.status == "question_review"
        assert overlaps and max(item.combined_score for item in overlaps) >= settings.overlap_auto_threshold
        assert any(item.relation_type == "cross_screenshot" and item.decision == "suggested" for item in cross_relations)
        assert duplicate_batch is not None and duplicate_batch.status == "ordering_review"
        assert duplicate_batch.found_question_count == 1
        assert duplicate_batch.high_confidence_count == 1
        assert any(item.relation_type == "same_batch_duplicate" and item.decision == "auto_merged" for item in duplicate_relations)
