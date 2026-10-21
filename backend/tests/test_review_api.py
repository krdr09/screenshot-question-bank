from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select

from app.config import Settings
from app.main import create_app
from app.models import CandidateImagePart, CandidateRelationship, Category, Course, ImportBatch, OrderingIssue, QuestionCandidate, ReviewAction, SourceImage
from app.ocr.base import OcrBox, OcrPageResult, Point
from app.ocr.mock import MockOcrEngine


def _ocr_result() -> OcrPageResult:
    texts = ["1.[单选题]", "API 重跑题干", "A. 甲", "B. 乙", "正确答案: B"]
    return OcrPageResult(
        page_width=120,
        page_height=200,
        boxes=[
            OcrBox(
                text=text,
                polygon=[Point(x=0, y=i * 25), Point(x=100, y=i * 25), Point(x=100, y=i * 25 + 12), Point(x=0, y=i * 25 + 12)],
                confidence=0.98,
            )
            for i, text in enumerate(texts)
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
            course = Course(name="课程")
            batch = ImportBatch(course=course, name="批次", source_relative_path="课程/批次", status="question_review", found_question_count=2, review_count=2)
            images = []
            for index in range(2):
                source = settings.data_dir / f"imports/{index}.png"
                source.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (120, 200), "white").save(source)
                images.append(SourceImage(batch=batch, source_relative_path=f"{index}.png", original_name=f"{index}.png", safe_name=f"{index}.png", relative_path=f"imports/{index}.png", sha256=str(index + 1) * 64, mime_type="image/png", width=120, height=200, file_size=source.stat().st_size, order_index=index, ocr_status="completed"))
            session.add_all([batch, *images])
            session.flush()
            candidates = []
            for index, image in enumerate(images):
                crop = settings.data_dir / f"crops/{index}.png"
                crop.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (120, 100), "white").save(crop)
                candidate = QuestionCandidate(batch_id=batch.id, source_image=image, question_type="single_choice", stem=f"候选{index}", options_json=[{"label": "A", "text": "甲"}, {"label": "B", "text": "乙"}], correct_answer_json="B" if index else None, raw_ocr_text=f"候选{index}", normalized_ocr_text=f"候选{index}", ocr_confidence=0.7, parse_confidence=0.6, display_mode="text", anomaly_flags_json=[{"code": "MISSING_ANSWER", "severity": "error", "message": "缺少答案"}], status="review")
                session.add(candidate)
                session.flush()
                session.add(CandidateImagePart(candidate_id=candidate.id, source_image_id=image.id, crop_path=f"crops/{index}.png", source_y_start=20, source_y_end=120, part_index=0))
                candidates.append(candidate)
            relationship = CandidateRelationship(batch_id=batch.id, primary_candidate_id=candidates[0].id, secondary_candidate_id=candidates[1].id, relation_type="cross_screenshot", decision="suggested", confidence=0.9, evidence_json={"merged_text_preview": "候选0\n候选1"}, status="pending")
            session.add(relationship)
            session.commit()
            return app, batch.id, candidates[0].id, candidates[1].id, images[0].id, relationship.id


def test_review_api_lists_filters_updates_and_serves_owned_images(tmp_path: Path) -> None:
    app, batch_id, first_id, second_id, first_image_id, _ = _app_and_seed(tmp_path)
    with TestClient(app) as client:
        listed = client.get("/api/review-items", params={"batch_id": batch_id})
        filtered = client.get("/api/review-items", params={"batch_id": batch_id, "anomaly_code": "MISSING_ANSWER", "severity": "error"})
        detail = client.get(f"/api/review-items/{first_id}")
        part_id = detail.json()["parts"][0]["id"] if detail.status_code == 200 else "missing"
        crop = client.get(f"/api/review-items/{first_id}/parts/{part_id}/content")
        source = client.get(f"/api/review-items/{first_id}/source-images/{first_image_id}/content")
        wrong_owner = client.get(f"/api/review-items/{second_id}/source-images/{first_image_id}/content")
        invalid = client.patch(f"/api/review-items/{first_id}", json={"options": [{"label": "A", "text": "甲"}, {"label": "C", "text": "丙"}], "correct_answer": "A"})
        saved = client.patch(f"/api/review-items/{first_id}", json={"stem": "人工修正", "correct_answer": "A", "display_mode": "text_first"})

    assert listed.status_code == 200 and len(listed.json()["items"]) == 2
    assert len(filtered.json()["items"]) == 2
    assert detail.json()["raw_ocr_text"] == "候选0"
    assert detail.json()["relationships"][0]["confidence"] == 0.9
    assert crop.status_code == source.status_code == 200
    assert wrong_owner.status_code == 404
    assert invalid.status_code == 422
    assert saved.status_code == 200 and saved.json()["stem"] == "人工修正"


def test_review_api_requires_confirmation_and_exposes_all_actions(tmp_path: Path) -> None:
    app, _, first_id, second_id, _, relationship_id = _app_and_seed(tmp_path)
    with TestClient(app) as client:
        unconfirmed = client.post(f"/api/review-items/{first_id}/delete", json={"confirmed": False})
        separated = client.post(
            f"/api/review-relationships/{relationship_id}/resolve",
            json={"decision": "separate", "confirmed": True},
        )
        client.patch(f"/api/review-items/{first_id}", json={"correct_answer": "A", "display_mode": "text_first"}).raise_for_status()
        confirmed = client.post(f"/api/review-items/{first_id}/confirm")
        undone = client.post(f"/api/review-items/{first_id}/undo")
        reordered = client.post(f"/api/review-items/{first_id}/reorder", json={"direction": "down", "confirmed": True})
        duplicate = client.post(f"/api/review-items/{second_id}/duplicate", json={"duplicate_of_id": first_id, "confirmed": True})
        rerun = client.post(f"/api/review-items/{first_id}/rerun-ocr", json={"confirmed": True})
        rerun_source = client.post(f"/api/review-items/{first_id}/rerun-source-ocr", json={"confirmed": True})
        merged = client.post(f"/api/review-relationships/{relationship_id}/resolve", json={"decision": "confirm", "confirmed": True})

    assert unconfirmed.status_code == 422
    assert separated.status_code == 200
    assert confirmed.status_code == undone.status_code == 200
    assert reordered.status_code == duplicate.status_code == 200
    assert rerun.status_code == rerun_source.status_code == 200
    assert merged.status_code == 409
    assert "刷新" in merged.json()["detail"]


def test_relationship_replay_returns_409_without_side_effects(tmp_path: Path) -> None:
    app, batch_id, first_id, second_id, _, relationship_id = _app_and_seed(tmp_path)
    with TestClient(app) as client:
        first = client.post(
            f"/api/review-relationships/{relationship_id}/resolve",
            json={"decision": "only_primary", "confirmed": True},
        )
        repeated = client.post(
            f"/api/review-relationships/{relationship_id}/resolve",
            json={"decision": "only_secondary", "confirmed": True},
        )

    assert first.status_code == 200
    assert repeated.status_code == 409
    assert "刷新" in repeated.json()["detail"]
    with app.state.database.session_factory() as session:
        relationship = session.get(CandidateRelationship, relationship_id)
        batch = session.get(ImportBatch, batch_id)
        assert (relationship.status, relationship.decision) == (
            "resolved",
            "kept_primary",
        )
        assert session.get(QuestionCandidate, first_id).status == "review"
        assert session.get(QuestionCandidate, second_id).status == "ignored"
        assert batch.review_count == 1
        assert session.scalar(
            select(func.count(ReviewAction.id)).where(
                ReviewAction.target_type == "relationship",
                ReviewAction.target_id == relationship_id,
            )
        ) == 1


def test_direct_merge_returns_409_for_resolved_relationship(tmp_path: Path) -> None:
    app, _, first_id, second_id, _, relationship_id = _app_and_seed(tmp_path)
    with TestClient(app) as client:
        client.post(
            f"/api/review-relationships/{relationship_id}/resolve",
            json={"decision": "separate", "confirmed": True},
        ).raise_for_status()
        response = client.post(
            f"/api/review-items/{first_id}/merge",
            json={
                "secondary_candidate_id": second_id,
                "relationship_id": relationship_id,
                "confirmed": True,
            },
        )

    assert response.status_code == 409


def test_exact_confirmed_merge_retry_remains_http_200(tmp_path: Path) -> None:
    app, _, first_id, _, _, relationship_id = _app_and_seed(tmp_path)
    with TestClient(app) as client:
        first = client.post(
            f"/api/review-relationships/{relationship_id}/resolve",
            json={"decision": "confirm", "confirmed": True},
        )
        repeated = client.post(
            f"/api/review-relationships/{relationship_id}/resolve",
            json={"decision": "confirm", "confirmed": True},
        )

    assert first.status_code == repeated.status_code == 200
    with app.state.database.session_factory() as session:
        assert session.scalar(
            select(func.count(ReviewAction.id)).where(
                ReviewAction.candidate_id == first_id,
                ReviewAction.action_type == "merge",
            )
        ) == 1


def test_repeated_title_validation_error_rolls_back_http_relationship_claim(
    tmp_path: Path,
) -> None:
    app, _, first_id, second_id, _, relationship_id = _app_and_seed(tmp_path)
    with app.state.database.session_factory() as session:
        first = session.get(QuestionCandidate, first_id)
        second = session.get(QuestionCandidate, second_id)
        relationship = session.get(CandidateRelationship, relationship_id)
        assert first is not None and second is not None and relationship is not None
        relationship.evidence_json = {
            **relationship.evidence_json,
            "continuation_mode": "repeated_title",
        }
        first.source_display_number = second.source_display_number = "7"
        first.question_type = second.question_type = "single_choice"
        first.stem = "前屏独有且后屏没有的题干"
        first.raw_ocr_text = "7.[单选题]\n前屏独有且后屏没有的题干\nA.甲"
        first.options_json = [{"label": "A", "text": "甲", "order_index": 0}]
        first.correct_answer_json = "B"
        second.stem = "示例命题完整内容"
        second.raw_ocr_text = (
            "7.[单选题]\n示例命题完整内容\nA.甲\nB.乙\n正确答案:B"
        )
        first.ocr_confidence = second.ocr_confidence = 0.99
        session.commit()

    with TestClient(app) as client:
        response = client.post(
            f"/api/review-relationships/{relationship_id}/resolve",
            json={"decision": "confirm", "confirmed": True},
        )
    assert response.status_code == 422

    with app.state.database.session_factory() as session:
        first = session.get(QuestionCandidate, first_id)
        second = session.get(QuestionCandidate, second_id)
        relationship = session.get(CandidateRelationship, relationship_id)
        assert first is not None and second is not None and relationship is not None
        assert (relationship.status, relationship.decision) == ("pending", "suggested")
        assert (first.status, second.status) == ("review", "review")
        assert session.scalar(select(func.count(CandidateImagePart.id))) == 2
        assert session.scalar(
            select(func.count(ReviewAction.id)).where(
                ReviewAction.target_type == "relationship",
                ReviewAction.target_id == relationship_id,
            )
        ) == 0


def test_relationship_resolution_preserves_404_and_422(tmp_path: Path) -> None:
    app, _, _, _, _, relationship_id = _app_and_seed(tmp_path)
    with TestClient(app) as client:
        missing = client.post(
            "/api/review-relationships/missing/resolve",
            json={"decision": "separate", "confirmed": True},
        )
        invalid = client.post(
            f"/api/review-relationships/{relationship_id}/resolve",
            json={"decision": "unsupported", "confirmed": True},
        )

    assert missing.status_code == 404
    assert invalid.status_code == 422


@pytest.mark.parametrize(
    ("method", "path_suffix", "json"),
    [
        ("POST", "confirm", None),
        ("POST", "ignore", {"confirmed": True}),
        ("POST", "delete", {"confirmed": True}),
    ],
    ids=["confirm", "ignore", "delete"],
)
def test_review_api_blocks_candidate_state_changes_until_relationship_is_resolved(
    tmp_path: Path,
    method: str,
    path_suffix: str,
    json: dict | None,
) -> None:
    app, _, _, second_id, _, relationship_id = _app_and_seed(tmp_path)

    with TestClient(app) as client:
        response = client.request(
            method,
            f"/api/review-items/{second_id}/{path_suffix}",
            json=json,
        )

    assert response.status_code == 422
    assert "先处理" in response.json()["detail"]
    with app.state.database.session_factory() as session:
        assert session.get(QuestionCandidate, second_id).status == "review"
        assert session.get(CandidateRelationship, relationship_id).status == "pending"


def test_review_api_updates_relationship_crops_atomically(tmp_path: Path) -> None:
    app, _, first_id, second_id, _, relationship_id = _app_and_seed(tmp_path)

    with TestClient(app) as client:
        primary_before = client.get(f"/api/review-items/{first_id}").json()
        secondary_before = client.get(f"/api/review-items/{second_id}").json()
        primary_part = primary_before["parts"][0]
        secondary_part = secondary_before["parts"][0]

        invalid = client.patch(
            f"/api/review-relationships/{relationship_id}/crops",
            json={
                "primary_parts": [
                    {
                        "id": primary_part["id"],
                        "part_index": 0,
                        "source_y_start": 30,
                        "source_y_end": 110,
                    }
                ],
                "secondary_parts": [
                    {
                        "id": secondary_part["id"],
                        "part_index": 0,
                        "source_y_start": 40,
                        "source_y_end": 999,
                    }
                ],
            },
        )

        assert invalid.status_code == 422
        assert client.get(f"/api/review-items/{first_id}").json()["parts"][0][
            "source_y_start"
        ] == 20
        assert client.get(f"/api/review-items/{second_id}").json()["parts"][0][
            "source_y_start"
        ] == 20

        updated = client.patch(
            f"/api/review-relationships/{relationship_id}/crops",
            json={
                "primary_parts": [
                    {
                        "id": primary_part["id"],
                        "part_index": 0,
                        "source_y_start": 30,
                        "source_y_end": 110,
                    }
                ],
                "secondary_parts": [
                    {
                        "id": secondary_part["id"],
                        "part_index": 0,
                        "source_y_start": 40,
                        "source_y_end": 130,
                    }
                ],
            },
        )

    assert updated.status_code == 200
    assert updated.json()["primary"]["parts"][0]["source_y_start"] == 30
    assert updated.json()["primary"]["parts"][0]["source_y_end"] == 110
    assert updated.json()["secondary"]["parts"][0]["source_y_start"] == 40
    assert updated.json()["secondary"]["parts"][0]["source_y_end"] == 130


def test_review_api_split_ignore_delete_and_relation_decisions(tmp_path: Path) -> None:
    app, _, first_id, second_id, _, relationship_id = _app_and_seed(tmp_path)
    with TestClient(app) as client:
        split = client.post(f"/api/review-items/{first_id}/split", json={"split_y": 70, "second_stem": "拆分题", "confirmed": True})
        separate = client.post(f"/api/review-relationships/{relationship_id}/resolve", json={"decision": "separate", "confirmed": True})
        ignored = client.post(f"/api/review-items/{second_id}/ignore", json={"confirmed": True})
        deleted = client.post(f"/api/review-items/{second_id}/delete", json={"confirmed": True})

    assert split.status_code == 200 and split.json()["stem"] == "拆分题"
    assert ignored.status_code == 200 and ignored.json()["status"] == "ignored"
    assert deleted.status_code == 200 and deleted.json()["status"] == "deleted"
    assert separate.status_code == 200 and separate.json()["decision"] == "kept_separate"


def test_global_review_queue_keeps_pending_relationships_and_ordering_issues_without_review_candidates(tmp_path: Path) -> None:
    app, batch_id, _, _, _, relationship_id = _app_and_seed(tmp_path)
    with app.state.database.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        batch.category = Category(course_id=batch.course_id, path="章节")
        for candidate in session.query(QuestionCandidate).all():
            candidate.status = "ready"
        issue = OrderingIssue(batch_id=batch_id, code="NEAR_TIE", message="仍需决定截图顺序", image_ids_json=[])
        session.add(issue)
        session.commit()
        issue_id = issue.id
    with TestClient(app) as client:
        response = client.get("/api/review-items")
        processed_before = client.get("/api/review-items", params={"handling_status": "processed"})
        matching_anomaly = client.get("/api/review-items", params={"anomaly_code": "MISSING_ANSWER"})
        unrelated_type = client.get("/api/review-items", params={"question_type": "true_false"})
        client.post(
            f"/api/review-relationships/{relationship_id}/resolve",
            json={"decision": "separate", "confirmed": True},
        ).raise_for_status()
        processed_after = client.get("/api/review-items", params={"handling_status": "processed"})

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert [item["id"] for item in response.json()["relationships"]] == [relationship_id]
    relationship = response.json()["relationships"][0]
    assert relationship["batch_id"] == batch_id
    assert relationship["batch_name"] == "批次"
    assert relationship["course_name"] == "课程"
    assert relationship["category_path"] == "章节"
    assert [item["id"] for item in response.json()["ordering_issues"]] == [issue_id]
    ordering_issue = response.json()["ordering_issues"][0]
    assert ordering_issue["batch_id"] == batch_id
    assert ordering_issue["batch_name"] == "批次"
    assert ordering_issue["course_name"] == "课程"
    assert ordering_issue["category_path"] == "章节"
    assert processed_before.json()["relationships"] == []
    assert processed_before.json()["ordering_issues"] == []
    assert [item["id"] for item in matching_anomaly.json()["relationships"]] == [relationship_id]
    assert matching_anomaly.json()["ordering_issues"] == []
    assert unrelated_type.json()["relationships"] == []
    assert unrelated_type.json()["ordering_issues"] == []
    assert [item["id"] for item in processed_after.json()["relationships"]] == [relationship_id]
    assert processed_after.json()["ordering_issues"] == []


def test_candidate_history_filters_transition_without_restricting_direct_lookup(
    tmp_path: Path,
) -> None:
    app, _, direct_ready_id, review_id, _, relationship_id = _app_and_seed(tmp_path)
    with app.state.database.session_factory() as session:
        session.delete(session.get(CandidateRelationship, relationship_id))
        session.get(QuestionCandidate, direct_ready_id).status = "ready"
        session.commit()

    with TestClient(app) as client:
        all_before = client.get(
            "/api/review-items", params={"handling_status": "all"}
        )
        direct_detail = client.get(f"/api/review-items/{direct_ready_id}")
        confirmed = client.post(f"/api/review-items/{review_id}/confirm")
        processed_after_confirm = client.get(
            "/api/review-items", params={"handling_status": "processed"}
        )
        all_after_confirm = client.get(
            "/api/review-items", params={"handling_status": "all"}
        )
        undone = client.post(f"/api/review-items/{review_id}/undo")
        pending_after_undo = client.get(
            "/api/review-items", params={"handling_status": "pending"}
        )
        processed_after_undo = client.get(
            "/api/review-items", params={"handling_status": "processed"}
        )
        all_after_undo = client.get(
            "/api/review-items", params={"handling_status": "all"}
        )

    assert all_before.status_code == direct_detail.status_code == 200
    assert [item["id"] for item in all_before.json()["items"]] == [review_id]
    assert direct_detail.json()["id"] == direct_ready_id
    assert confirmed.status_code == undone.status_code == 200
    assert [
        item["id"] for item in processed_after_confirm.json()["items"]
    ] == [review_id]
    assert [item["id"] for item in all_after_confirm.json()["items"]] == [review_id]
    assert [item["id"] for item in pending_after_undo.json()["items"]] == [review_id]
    assert processed_after_undo.json()["items"] == []
    assert [item["id"] for item in all_after_undo.json()["items"]] == [review_id]


def test_review_api_exposes_required_delete_method(tmp_path: Path) -> None:
    app, _, first_id, _, _, relationship_id = _app_and_seed(tmp_path)
    with app.state.database.session_factory() as session:
        session.delete(session.get(CandidateRelationship, relationship_id))
        session.commit()
    with TestClient(app) as client:
        missing_confirmation = client.request("DELETE", f"/api/review-items/{first_id}")
        invalid_confirmation = client.request(
            "DELETE", f"/api/review-items/{first_id}", json={"confirmed": False}
        )
        deleted = client.request(
            "DELETE", f"/api/review-items/{first_id}", json={"confirmed": True}
        )

    assert missing_confirmation.status_code == 422
    assert invalid_confirmation.status_code == 422
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "deleted"


def test_review_api_exposes_required_merge_method(tmp_path: Path) -> None:
    app, _, first_id, second_id, _, relationship_id = _app_and_seed(tmp_path)
    with app.state.database.session_factory() as session:
        session.delete(session.get(CandidateRelationship, relationship_id))
        session.commit()
    with TestClient(app) as client:
        missing_confirmation = client.post(
            f"/api/review-items/{first_id}/merge",
            json={
                "secondary_candidate_id": second_id,
            },
        )
        merged = client.post(
            f"/api/review-items/{first_id}/merge",
            json={
                "secondary_candidate_id": second_id,
                "confirmed": True,
            },
        )

    assert missing_confirmation.status_code == 422
    assert merged.status_code == 200
    assert merged.json()["id"] == first_id
    assert len(merged.json()["parts"]) == 2
    assert merged.json()["relationships"][0]["relation_type"] == "manual_merge"
    assert merged.json()["relationships"][0]["status"] == "resolved"


def test_review_api_rejects_inactive_manual_relationship_targets(
    tmp_path: Path,
) -> None:
    app, _, first_id, second_id, _, relationship_id = _app_and_seed(tmp_path)
    with app.state.database.session_factory() as session:
        session.delete(session.get(CandidateRelationship, relationship_id))
        session.get(QuestionCandidate, second_id).status = "ignored"
        session.commit()

    with TestClient(app) as client:
        merged = client.post(
            f"/api/review-items/{first_id}/merge",
            json={"secondary_candidate_id": second_id, "confirmed": True},
        )
        duplicate = client.post(
            f"/api/review-items/{first_id}/duplicate",
            json={"duplicate_of_id": second_id, "confirmed": True},
        )

    assert merged.status_code == 422
    assert duplicate.status_code == 422
    assert "有效候选题" in merged.json()["detail"]
    assert "有效候选题" in duplicate.json()["detail"]


def test_review_api_exposes_unfiltered_active_batch_context(tmp_path: Path) -> None:
    app, batch_id, first_id, second_id, _, relationship_id = _app_and_seed(tmp_path)
    with app.state.database.session_factory() as session:
        session.delete(session.get(CandidateRelationship, relationship_id))
        session.get(QuestionCandidate, second_id).status = "ready"
        session.commit()

    with TestClient(app) as client:
        pending = client.get("/api/review-items", params={"batch_id": batch_id})
        context = client.get(f"/api/review-items/{first_id}/batch-context")
        missing = client.get("/api/review-items/missing-candidate/batch-context")

    assert pending.status_code == 200
    assert [item["id"] for item in pending.json()["items"]] == [first_id]
    assert context.status_code == 200
    assert [item["id"] for item in context.json()["items"]] == [first_id, second_id]
    assert all(
        set(item) == {"id", "batch_id", "stem", "status"}
        for item in context.json()["items"]
    )
    assert context.json()["items"][1]["status"] == "ready"
    assert missing.status_code == 404
