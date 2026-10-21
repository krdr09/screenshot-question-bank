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
    PracticeSession,
    QuestionCandidate,
    SourceImage,
    WrongBookEntry,
)
from app.ocr.base import OcrBox, OcrPageResult, Point
from app.ocr.mock import MockOcrEngine
from app.services.library.deletion_recovery import BatchDeletionJournal


def _ocr_result() -> OcrPageResult:
    texts = ["1.[单选题]", "重跑后的题干", "A. 新甲", "B. 新乙", "正确答案: B", "答案解析: 重跑解析"]
    return OcrPageResult(
        page_width=120,
        page_height=100,
        boxes=[
            OcrBox(
                text=text,
                polygon=[Point(x=0, y=index * 14 + 12), Point(x=110, y=index * 14 + 12), Point(x=110, y=index * 14 + 22), Point(x=0, y=index * 14 + 22)],
                confidence=0.99,
            )
            for index, text in enumerate(texts)
        ],
        raw_output={"mock": True},
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
    app = create_app(settings, MockOcrEngine(default=_ocr_result()))
    with TestClient(app):
        with app.state.database.session_factory() as session:
            course = Course(name="医学")
            respiratory = Category(course=course, path="内科/呼吸")
            circulation = Category(course=course, path="内科/循环")
            other = Course(name="法规")
            other_category = Category(course=other, path="第一章")
            batch = ImportBatch(course=course, category=respiratory, name="正式批次", source_relative_path="医学/正式批次", status="ready")
            session.add_all([batch, circulation, other_category])
            session.flush()
            source_path = settings.data_dir / "imports/source.png"
            crop_path = settings.data_dir / "crops/question.png"
            source_path.parent.mkdir(parents=True, exist_ok=True)
            crop_path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (120, 200), "white").save(source_path)
            Image.new("RGB", (120, 100), "white").save(crop_path)
            image = SourceImage(
                batch=batch,
                source_relative_path="用户目录/原图.png",
                original_name="原图.png",
                safe_name="source.png",
                relative_path="imports/source.png",
                sha256="a" * 64,
                mime_type="image/png",
                width=120,
                height=200,
                file_size=source_path.stat().st_size,
                order_index=0,
                ocr_status="completed",
            )
            session.add(image)
            session.flush()
            ready = QuestionCandidate(
                batch_id=batch.id,
                category_id=respiratory.id,
                source_image=image,
                source_display_number="来源第 1 题",
                question_type="single_choice",
                stem="原题干",
                options_json=[{"label": "A", "text": "甲"}, {"label": "B", "text": "乙"}],
                correct_answer_json="A",
                explanation="原解析",
                raw_ocr_text="原始 OCR 肺炎",
                normalized_ocr_text="原始 ocr 肺炎",
                ocr_confidence=0.98,
                parse_confidence=0.97,
                display_mode="text_first",
                anomaly_flags_json=[],
                status="ready",
            )
            hidden = QuestionCandidate(
                batch_id=batch.id,
                category_id=respiratory.id,
                source_image=image,
                question_type="single_choice",
                stem="待检查",
                options_json=[{"label": "A", "text": "甲"}, {"label": "B", "text": "乙"}],
                raw_ocr_text="待检查",
                normalized_ocr_text="待检查",
                ocr_confidence=0.5,
                parse_confidence=0.5,
                display_mode="text_first",
                anomaly_flags_json=[],
                status="review",
            )
            session.add_all([ready, hidden])
            session.flush()
            session.add_all([
                CandidateImagePart(candidate_id=ready.id, source_image_id=image.id, crop_path="crops/question.png", source_y_start=20, source_y_end=120, part_index=0),
                WrongBookEntry(question_id=ready.id),
            ])
            session.commit()
            return app, ready.id, hidden.id, course.id, respiratory.id, circulation.id, other_category.id, batch.id, image.id


def test_library_api_lists_metadata_questions_and_owned_images(tmp_path: Path) -> None:
    app, ready_id, hidden_id, course_id, _, _, _, batch_id, image_id = _app_and_seed(tmp_path)
    with TestClient(app) as client:
        courses = client.get("/api/courses")
        batches = client.get("/api/batches", params={"course_id": course_id})
        listed = client.get("/api/questions", params={"course_id": course_id, "batch_id": batch_id, "search": "肺炎"})
        hidden = client.get(f"/api/questions/{hidden_id}")
        detail = client.get(f"/api/questions/{ready_id}")
        part_id = detail.json()["parts"][0]["id"] if detail.status_code == 200 else "missing"
        crop = client.get(f"/api/questions/{ready_id}/parts/{part_id}/content")
        source = client.get(f"/api/questions/{ready_id}/source-images/{image_id}/content")

    assert courses.status_code == 200 and courses.json()["items"][0]["name"] == "医学"
    assert batches.status_code == 200 and batches.json()["items"][0]["name"] == "正式批次"
    assert listed.status_code == 200 and listed.json()["total"] == 1
    assert listed.json()["items"][0]["in_wrong_book"] is True
    assert hidden.status_code == 404
    assert detail.json()["source_display_number"] == "来源第 1 题"
    assert detail.json()["internal_uuid"] == ready_id
    assert crop.status_code == source.status_code == 200


def test_library_api_edits_category_and_rejects_cross_course_category(tmp_path: Path) -> None:
    app, ready_id, _, course_id, respiratory_id, circulation_id, other_category_id, batch_id, _ = _app_and_seed(tmp_path)
    with TestClient(app) as client:
        invalid = client.patch(f"/api/questions/{ready_id}", json={"category_id": other_category_id})
        saved = client.patch(
            f"/api/questions/{ready_id}",
            json={
                "category_id": circulation_id,
                "stem": "人工编辑题干",
                "options": [{"label": "A", "text": "新甲"}, {"label": "B", "text": "新乙"}],
                "correct_answer": "B",
                "question_type": "single_choice",
                "display_mode": "image_first",
                "explanation": "人工解析",
            },
        )
        questions_in_circulation = client.get(
            "/api/questions",
            params={"course_id": course_id, "category_id": circulation_id},
        )
        batches_in_circulation = client.get(
            "/api/batches",
            params={"course_id": course_id, "category_id": circulation_id},
        )
        questions_in_respiratory = client.get(
            "/api/questions",
            params={"course_id": course_id, "category_id": respiratory_id},
        )
        batches_in_respiratory = client.get(
            "/api/batches",
            params={"course_id": course_id, "category_id": respiratory_id},
        )

    assert invalid.status_code == 422
    assert saved.status_code == 200
    assert saved.json()["category_id"] == circulation_id
    assert saved.json()["category_path"] == "内科/循环"
    assert saved.json()["stem"] == "人工编辑题干"
    assert saved.json()["correct_answer"] == "B"
    assert questions_in_circulation.status_code == 200
    assert questions_in_circulation.json()["total"] == 1
    assert questions_in_circulation.json()["items"][0]["batch_id"] == batch_id
    assert batches_in_circulation.status_code == 200
    circulation_batches = batches_in_circulation.json()["items"]
    assert len(circulation_batches) == 1
    assert circulation_batches[0]["id"] == batch_id
    assert circulation_batches[0]["question_count"] == 1
    assert circulation_batches[0]["category_id"] == respiratory_id
    assert circulation_batches[0]["category_path"] == "内科/呼吸"
    assert questions_in_respiratory.status_code == 200
    assert questions_in_respiratory.json()["total"] == 0
    assert batches_in_respiratory.status_code == 200
    assert batches_in_respiratory.json()["items"] == []


def test_library_api_reruns_mock_ocr_and_soft_deletes_with_confirmation(tmp_path: Path) -> None:
    app, ready_id, _, _, _, _, _, _, _ = _app_and_seed(tmp_path)
    with TestClient(app) as client:
        unconfirmed_ocr = client.post(f"/api/questions/{ready_id}/rerun-ocr", json={"confirmed": False})
        rerun = client.post(f"/api/questions/{ready_id}/rerun-ocr", json={"confirmed": True})
        unconfirmed_delete = client.request("DELETE", f"/api/questions/{ready_id}", json={"confirmed": False})
        deleted = client.request("DELETE", f"/api/questions/{ready_id}", json={"confirmed": True})
        missing = client.get(f"/api/questions/{ready_id}")

    assert unconfirmed_ocr.status_code == unconfirmed_delete.status_code == 422
    assert rerun.status_code == 200 and rerun.json()["stem"] == "重跑后的题干"
    assert rerun.json()["correct_answer"] == "B"
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True
    assert missing.status_code == 404


def test_batch_delete_api_reports_cleanup_pending(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app, _, _, _, _, _, _, batch_id, _ = _app_and_seed(tmp_path)
    real_rmtree = __import__("shutil").rmtree

    def fail_operation_cleanup(path, *args, **kwargs):
        candidate = Path(path)
        if ".delete-staging" in candidate.parts:
            raise OSError("injected deletion cleanup failure")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(
        "app.services.library.deletion_recovery.shutil.rmtree",
        fail_operation_cleanup,
    )
    with TestClient(app) as client:
        response = client.request(
            "DELETE",
            f"/api/batches/{batch_id}",
            json={"confirmed": True},
        )

    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert response.json()["cleanup_pending"] is True

    monkeypatch.undo()
    with app.state.database.session_factory() as session:
        report = BatchDeletionJournal(app.state.settings.data_dir).recover(session)
    assert report.cleaned_operations == 1


@pytest.mark.parametrize(
    "stored_value",
    ["{", b"\xff"],
    ids=["invalid-json-syntax", "invalid-utf8-blob"],
)
def test_batch_delete_api_sanitizes_corrupt_practice_snapshot_before_staging(
    tmp_path: Path,
    stored_value: object,
) -> None:
    app, ready_id, _, _, _, _, _, batch_id, _ = _app_and_seed(tmp_path)
    with app.state.database.session_factory() as session:
        practice = PracticeSession(
            feedback_mode="deferred",
            order_mode="sequential",
            selection_json={"batch_id": batch_id},
            requested_count=1,
            actual_count=1,
            limit_adjusted=False,
            status="active",
        )
        item = PracticeItem(
            session=practice,
            question_id=ready_id,
            order_index=0,
            question_snapshot_json={"question_id": ready_id},
            answer_snapshot_json="A",
            explanation_snapshot=None,
            image_parts_snapshot_json=[],
        )
        session.add(item)
        session.commit()
        item_id = item.id
    with app.state.database.engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE practice_items SET image_parts_snapshot_json = ? WHERE id = ?",
            (stored_value, item_id),
        )

    with TestClient(app) as client:
        response = client.request(
            "DELETE",
            f"/api/batches/{batch_id}",
            json={"confirmed": True},
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "练习图片快照无效，已取消删除"}
    with app.state.database.session_factory() as session:
        assert session.get(ImportBatch, batch_id) is not None
        assert session.get(QuestionCandidate, ready_id) is not None
    with app.state.database.engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT id FROM practice_items WHERE id = ?", (item_id,)
        ).scalar_one() == item_id
    assert (app.state.settings.data_dir / "imports/source.png").is_file()
    assert (app.state.settings.data_dir / "crops/question.png").is_file()
    assert not (app.state.settings.data_dir / ".delete-staging").exists()
