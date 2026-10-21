import hashlib
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.ocr.mock import MockOcrEngine
from app.testing.synthetic_screenshots import generate_parsing_screenshot_set


def _wait_until_processed(client: TestClient, batch_id: str) -> dict:
    deadline = time.monotonic() + 5
    while True:
        progress = client.get(f"/api/import-jobs/{batch_id}").json()
        if progress["processed_count"] == 3:
            return progress
        assert time.monotonic() < deadline, progress
        time.sleep(0.02)


def _create_session(
    client: TestClient,
    question_ids: list[str],
    feedback_mode: str,
) -> dict:
    response = client.post(
        "/api/practice-sessions",
        json={
            "selection": {"manual_question_ids": question_ids},
            "order_mode": "sequential",
            "feedback_mode": feedback_mode,
        },
    )
    assert response.status_code == 201, response.text
    assert "correct_answer" not in response.text
    assert "explanation" not in response.text
    return response.json()


def _answer(client: TestClient, session_id: str, item_id: str, value) -> dict:
    response = client.post(
        f"/api/practice-sessions/{session_id}/answers",
        json={"practice_item_id": item_id, "selected_answer": value},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _assert_all_crops_are_safe_and_readable(
    client: TestClient, practices: list[dict]
) -> None:
    urls = [
        part["url"]
        for practice in practices
        for item in practice["items"]
        for part in item["image_parts"]
    ]
    assert urls
    assert all(url.startswith("/api/practice-sessions/") for url in urls)
    assert all("source-images" not in url for url in urls)
    for url in urls:
        response = client.get(url)
        assert response.status_code == 200, (url, response.text)
        assert response.headers["content-type"].startswith("image/")


def test_synthetic_import_practice_grading_wrong_book_and_repractice_flow(
    tmp_path: Path,
) -> None:
    pages = generate_parsing_screenshot_set(tmp_path / "fixtures")
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
        frontend_dist=tmp_path / "missing-dist",
        _env_file=None,
    )
    app = create_app(settings, engine)

    with TestClient(app) as client:
        created = client.post(
            "/api/import-jobs",
            json={
                "course_name": "阶段八合成课程",
                "category_path": "Mock OCR/刷题流程",
                "batch_name": "两道单选一道判断",
                "source_relative_path": "阶段八合成课程/Mock OCR/刷题流程",
            },
        )
        created.raise_for_status()
        batch_id = created.json()["id"]
        multipart = [
            (
                "files",
                (page.image_path.name, page.image_path.read_bytes(), "image/png"),
            )
            for page in pages
        ] + [
            (
                "relative_paths",
                (None, f"阶段八合成课程/Mock OCR/{page.image_path.name}"),
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

        ready_response = client.get(
            "/api/practice-candidates", params={"batch_id": batch_id}
        )
        ready_response.raise_for_status()
        ready = ready_response.json()
        assert ready["total"] == 3
        singles = [
            item for item in ready["items"] if item["question_type"] == "single_choice"
        ]
        boolean = next(
            item for item in ready["items"] if item["question_type"] == "true_false"
        )
        choice_with_e = next(item for item in singles if "第五项" in item["stem"])
        other_choice = next(item for item in singles if item["id"] != choice_with_e["id"])

        immediate = _create_session(
            client, [choice_with_e["id"]], "immediate"
        )
        immediate_item = immediate["items"][0]
        immediate_feedback = _answer(
            client, immediate["id"], immediate_item["id"], "A"
        )
        assert immediate_feedback["is_correct"] is False
        assert immediate_feedback["correct_answer"] == "E"
        client.post(f"/api/practice-sessions/{immediate['id']}/submit").raise_for_status()

        deferred = _create_session(
            client,
            [choice_with_e["id"], boolean["id"], other_choice["id"]],
            "deferred",
        )
        deferred_items = {item["question_id"]: item for item in deferred["items"]}
        correct_saved = _answer(
            client,
            deferred["id"],
            deferred_items[choice_with_e["id"]]["id"],
            "E",
        )
        wrong_saved = _answer(
            client,
            deferred["id"],
            deferred_items[boolean["id"]]["id"],
            False,
        )
        assert "is_correct" not in correct_saved
        assert "correct_answer" not in wrong_saved
        submitted = client.post(
            f"/api/practice-sessions/{deferred['id']}/submit"
        )
        submitted.raise_for_status()
        result = submitted.json()
        assert {
            key: result[key]
            for key in (
                "total_count",
                "correct_count",
                "incorrect_count",
                "unanswered_count",
                "accuracy",
            )
        } == {
            "total_count": 3,
            "correct_count": 1,
            "incorrect_count": 1,
            "unanswered_count": 1,
            "accuracy": 33.33,
        }

        wrong_list = client.get("/api/wrong-questions").json()
        first_wrong = next(
            item
            for item in wrong_list["items"]
            if item["question_id"] == choice_with_e["id"]
        )
        assert first_wrong["error_count"] >= 1
        client.post(
            f"/api/wrong-questions/{choice_with_e['id']}/master"
        ).raise_for_status()
        hidden_ids = {
            item["question_id"]
            for item in client.get("/api/wrong-questions").json()["items"]
        }
        assert choice_with_e["id"] not in hidden_ids
        client.post(
            f"/api/wrong-questions/{choice_with_e['id']}/restore"
        ).raise_for_status()
        restored_ids = {
            item["question_id"]
            for item in client.get("/api/wrong-questions").json()["items"]
        }
        assert choice_with_e["id"] in restored_ids

        repractice = _create_session(
            client, [choice_with_e["id"]], "immediate"
        )
        _assert_all_crops_are_safe_and_readable(
            client, [immediate, deferred, repractice]
        )
