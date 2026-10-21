from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import (
    Attempt,
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
from app.ocr.mock import MockOcrEngine


def test_wrong_book_filters_counts_mastery_restore_and_review_fields(tmp_path: Path) -> None:
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
            course = Course(name="错题课程")
            category = Category(course=course, path="错题章节")
            batch = ImportBatch(
                course=course,
                category=category,
                name="错题批次",
                source_relative_path="错题课程/错题章节/错题批次",
                status="ready",
            )
            session.add(batch)
            session.flush()
            image = SourceImage(
                batch=batch,
                source_relative_path="wrong.png",
                original_name="wrong.png",
                safe_name="wrong.png",
                relative_path="imports/wrong.png",
                sha256="7" * 64,
                mime_type="image/png",
                width=100,
                height=100,
                file_size=1,
                order_index=0,
                ocr_status="completed",
            )
            session.add(image)
            session.flush()
            questions = []
            for index in range(2):
                question = QuestionCandidate(
                    batch_id=batch.id,
                    category_id=category.id,
                    source_image=image,
                    source_display_number=str(index + 1),
                    question_type="single_choice",
                    stem=f"错题 {index + 1}",
                    options_json=[{"label": "A", "text": "甲"}, {"label": "B", "text": "乙"}],
                    correct_answer_json="A",
                    explanation=f"错题 {index + 1} 解析",
                    raw_ocr_text="错题",
                    normalized_ocr_text="错题",
                    ocr_confidence=1,
                    parse_confidence=1,
                    display_mode="text",
                    anomaly_flags_json=[],
                    status="ready",
                )
                session.add(question)
                questions.append(question)
            session.flush()
            session.add(
                CandidateImagePart(
                    candidate_id=questions[0].id,
                    source_image_id=image.id,
                    crop_path="crops/wrong.png",
                    source_y_start=0,
                    source_y_end=50,
                    part_index=0,
                )
            )
            session.add_all(
                [
                    WrongBookEntry(question_id=questions[0].id),
                    WrongBookEntry(question_id=questions[1].id, mastered_at=questions[1].created_at),
                ]
            )
            for number in range(2):
                practice = PracticeSession(
                    feedback_mode="immediate",
                    order_mode="sequential",
                    selection_json={},
                    actual_count=1,
                    limit_adjusted=False,
                    status="submitted",
                )
                item = PracticeItem(
                    session=practice,
                    question_id=questions[0].id,
                    order_index=0,
                    question_snapshot_json={"question_type": "single_choice", "options": questions[0].options_json},
                    answer_snapshot_json="A",
                    image_parts_snapshot_json=[],
                )
                session.add_all(
                    [
                        practice,
                        item,
                        Attempt(
                            session=practice,
                            item=item,
                            selected_answer_json="B",
                            is_correct=False,
                            was_unanswered=False,
                        ),
                    ]
                )
            session.commit()
            active_id = questions[0].id
            mastered_id = questions[1].id
            course_id = course.id
            category_id = category.id
            batch_id = batch.id

        active = client.get(
            "/api/wrong-questions",
            params={"course_id": course_id, "category_id": category_id, "batch_id": batch_id},
        )
        included = client.get("/api/wrong-questions", params={"include_mastered": True})
        mastered = client.post(f"/api/wrong-questions/{active_id}/master")
        hidden = client.get("/api/wrong-questions")
        restored = client.post(f"/api/wrong-questions/{active_id}/restore")
        visible_again = client.get("/api/wrong-questions")
        missing = client.post("/api/wrong-questions/missing/restore")
        openapi = client.get("/openapi.json").json()

    assert active.status_code == 200
    assert active.json()["total"] == 1
    item = active.json()["items"][0]
    assert item["question_id"] == active_id
    assert item["error_count"] == 2
    assert item["correct_answer"] == "A"
    assert item["explanation"] == "错题 1 解析"
    assert item["image_parts"][0]["url"].startswith(f"/api/questions/{active_id}/parts/")
    assert {entry["question_id"] for entry in included.json()["items"]} == {active_id, mastered_id}
    assert mastered.status_code == restored.status_code == 200
    assert hidden.json()["total"] == 0
    assert visible_again.json()["items"][0]["question_id"] == active_id
    assert missing.status_code == 404
    for path in (
        "/api/wrong-questions/{question_id}/master",
        "/api/wrong-questions/{question_id}/restore",
    ):
        schema = openapi["paths"][path]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema["$ref"].endswith("/WrongBookMutationResponse")
