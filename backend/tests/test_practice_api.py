from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import Category, Course, ImportBatch, QuestionCandidate, SourceImage
from app.ocr.mock import MockOcrEngine


def test_practice_candidate_api_is_filterable_and_never_leaks_answers(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing",
        _env_file=None,
    )
    app = create_app(settings, MockOcrEngine())
    with TestClient(app) as client:
        with app.state.database.session_factory() as session:
            course = Course(name="API 课程")
            category = Category(course=course, path="第一章")
            batch = ImportBatch(
                course=course,
                category=category,
                name="API 批次",
                source_relative_path="API 课程/第一章/API 批次",
                status="ready",
            )
            session.add(batch)
            session.flush()
            image = SourceImage(
                batch=batch,
                source_relative_path="one.png",
                original_name="one.png",
                safe_name="one.png",
                relative_path="imports/one.png",
                sha256="f" * 64,
                mime_type="image/png",
                width=100,
                height=100,
                file_size=1,
                order_index=0,
                ocr_status="completed",
            )
            question = QuestionCandidate(
                batch_id=batch.id,
                category_id=category.id,
                source_image=image,
                source_display_number="重复题号 1",
                question_type="single_choice",
                stem="API 题干",
                options_json=[{"label": "A", "text": "甲"}, {"label": "B", "text": "乙"}],
                correct_answer_json="B",
                explanation="不能提前泄露的解析",
                raw_ocr_text="API OCR",
                normalized_ocr_text="api ocr",
                ocr_confidence=1,
                parse_confidence=1,
                display_mode="text",
                anomaly_flags_json=[],
                status="ready",
            )
            session.add_all([image, question])
            session.commit()

        response = client.get(
            "/api/practice-candidates",
            params={"course_id": course.id, "question_type": "single_choice"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["id"] == question.id
    assert payload["items"][0]["batch_position"] == 1
    assert "correct_answer" not in response.text
    assert "不能提前泄露的解析" not in response.text
