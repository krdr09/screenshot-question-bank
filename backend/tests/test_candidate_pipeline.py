import hashlib
import time
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.config import Settings
from app.main import create_app
from app.models import QuestionCandidate
from app.ocr.mock import MockOcrEngine
from app.testing.synthetic_screenshots import generate_parsing_screenshot_set


@pytest.mark.parametrize(
    ("threshold", "expected_statuses", "high_count", "review_count"),
    [
        (None, ["review", "ready", "review"], 1, 2),
        (0.8, ["ready", "ready", "review"], 2, 1),
        (0.98, ["review", "ready", "review"], 1, 2),
        (0.99, ["review", "review", "review"], 0, 3),
    ],
)
def test_mock_worker_persists_candidates_using_configured_threshold_with_safe_crops(
    tmp_path: Path,
    threshold: float | None,
    expected_statuses: list[str],
    high_count: int,
    review_count: int,
) -> None:
    pages = generate_parsing_screenshot_set(tmp_path / "fixtures")
    first_result = pages[0].ocr_result.model_copy(
        update={
            "boxes": [
                box.model_copy(update={"confidence": 0.979})
                for box in pages[0].ocr_result.boxes
            ]
        }
    )
    results_by_sha256 = {
        hashlib.sha256(page.image_path.read_bytes()).hexdigest(): page.ocr_result
        for page in pages
    }
    results_by_sha256[
        hashlib.sha256(pages[0].image_path.read_bytes()).hexdigest()
    ] = first_result
    engine = MockOcrEngine(results_by_sha256=results_by_sha256)
    threshold_setting = (
        {} if threshold is None else {"OCR_CONFIDENCE_THRESHOLD": threshold}
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=True,
        frontend_dist=tmp_path / "missing-dist",
        _env_file=None,
        **threshold_setting,
    )
    app = create_app(settings, ocr_engine=engine)

    with TestClient(app) as client:
        created = client.post(
            "/api/import-jobs",
            json={
                "course_name": "合成课程",
                "category_path": "解析样本",
                "batch_name": "阶段4",
                "source_relative_path": "合成课程/解析样本/阶段4",
            },
        ).json()
        multipart = [
            ("files", (page.image_path.name, page.image_path.read_bytes(), "image/png"))
            for page in pages
        ] + [
            (
                "relative_paths",
                (None, f"合成课程/解析样本/阶段4/{page.image_path.name}"),
            )
            for page in pages
        ]
        client.post(
            f"/api/import-jobs/{created['id']}/files", files=multipart
        ).raise_for_status()
        client.post(f"/api/import-jobs/{created['id']}/process").raise_for_status()
        deadline = time.monotonic() + 3
        while True:
            progress = client.get(f"/api/import-jobs/{created['id']}").json()
            if progress["status"] == "ordering_review":
                break
            assert time.monotonic() < deadline, progress
            time.sleep(0.02)

        with app.state.database.session_factory() as session:
            candidates = list(
                session.scalars(
                    select(QuestionCandidate).order_by(QuestionCandidate.created_at)
                )
            )
            assert len(candidates) == 3
            single, true_false, review = candidates
            assert UUID(single.id)
            assert UUID(review.id) != UUID(single.id)
            assert single.source_display_number == review.source_display_number == "1"
            assert single.question_type == "single_choice"
            assert [option["label"] for option in single.options_json] == list("ABCDE")
            assert single.correct_answer_json == "E"
            assert true_false.correct_answer_json is True
            assert [item.status for item in candidates] == expected_statuses
            assert {flag["code"] for flag in review.anomaly_flags_json} >= {
                "MISSING_ANSWER"
            }

            def anomaly_codes(candidate: QuestionCandidate) -> set[str]:
                return {flag["code"] for flag in candidate.anomaly_flags_json}

            assert ("LOW_OCR_CONFIDENCE" in anomaly_codes(single)) == (
                threshold is None or threshold >= 0.98
            )
            assert ("LOW_OCR_CONFIDENCE" in anomaly_codes(true_false)) == (
                threshold == 0.99
            )
            assert all(candidate.image_parts for candidate in candidates)
            for candidate in candidates:
                part = candidate.image_parts[0]
                assert not Path(part.crop_path).is_absolute()
                assert (settings.data_dir / part.crop_path).is_file()
                assert part.source_y_start < part.source_y_end

    assert progress["found_question_count"] == 3
    assert progress["high_confidence_count"] == high_count
    assert progress["review_count"] == review_count
