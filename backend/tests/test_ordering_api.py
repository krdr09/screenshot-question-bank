from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.config import Settings
from app.main import create_app
from app.models import (
    CandidateImagePart,
    CandidateRelationship,
    Course,
    ImportBatch,
    OrderingIssue,
    QuestionCandidate,
    ScreenshotOverlap,
    SourceImage,
)
from app.ocr.mock import MockOcrEngine
import app.services.ordering_exclusions as ordering_exclusions


def seed_ordering_job(app, settings: Settings) -> tuple[str, str, str]:
    image_dir = settings.data_dir / "imports" / "job"
    image_dir.mkdir(parents=True)
    paths = [image_dir / "one.png", image_dir / "two.png"]
    Image.new("RGB", (80, 120), "red").save(paths[0])
    Image.new("RGB", (80, 120), "blue").save(paths[1])
    with app.state.database.session_factory() as session:
        course = Course(name="顺序课程")
        session.add(course)
        session.flush()
        batch = ImportBatch(
            course_id=course.id,
            name="待确认",
            source_relative_path="顺序课程/待确认",
            status="ordering_review",
            uploaded_count=2,
            processed_count=2,
        )
        session.add(batch)
        session.flush()
        images = []
        for index, path in enumerate(paths):
            image = SourceImage(
                batch_id=batch.id,
                source_relative_path=f"arbitrary/{path.name}",
                original_name=path.name,
                safe_name=path.name,
                relative_path=path.relative_to(settings.data_dir).as_posix(),
                sha256=str(index + 1) * 64,
                mime_type="image/png",
                width=80,
                height=120,
                file_size=path.stat().st_size,
                order_index=index,
                ocr_status="completed",
            )
            images.append(image)
        session.add_all(images)
        session.flush()
        session.add(
            ScreenshotOverlap(
                batch_id=batch.id,
                source_image_id=images[0].id,
                target_image_id=images[1].id,
                text_score=0.71,
                visual_score=0.81,
                combined_score=0.75,
                overlap_ratio=0.3,
                matched_text_json=["重叠文字"],
            )
        )
        session.add(
            OrderingIssue(
                batch_id=batch.id,
                code="NEAR_TIE",
                message="候选接近",
                image_ids_json=[images[0].id, images[1].id],
            )
        )
        session.commit()
        return batch.id, images[0].id, images[1].id


def seed_candidate(
    app,
    batch_id: str,
    primary_image_id: str,
    *,
    part_image_ids: tuple[str, ...] = (),
    status: str = "ready",
    stem: str = "Ordering exclusion test question",
) -> str:
    with app.state.database.session_factory() as session:
        candidate = QuestionCandidate(
            batch_id=batch_id,
            source_image_id=primary_image_id,
            question_type="single_choice",
            stem=stem,
            options_json=[{"label": "A", "text": "One"}, {"label": "B", "text": "Two"}],
            correct_answer_json="A",
            raw_ocr_text=stem,
            normalized_ocr_text=stem,
            ocr_confidence=0.98,
            parse_confidence=0.97,
            display_mode="text_first",
            anomaly_flags_json=[{"code": "EXISTING", "severity": "warning", "message": "Preserve"}],
            status=status,
        )
        session.add(candidate)
        session.flush()
        for index, image_id in enumerate(part_image_ids):
            session.add(
                CandidateImagePart(
                    candidate_id=candidate.id,
                    source_image_id=image_id,
                    crop_path=f"crops/{candidate.id}-{index}.png",
                    source_y_start=index * 10,
                    source_y_end=index * 10 + 10,
                    part_index=index,
                )
            )
        session.commit()
        return candidate.id


def seed_external_batch(
    app,
    *,
    name: str,
    status: str = "ready",
    counts: tuple[int, int, int] = (0, 0, 0),
) -> tuple[str, str]:
    with app.state.database.session_factory() as session:
        course = session.scalar(select(Course).order_by(Course.created_at))
        assert course is not None
        batch = ImportBatch(
            course_id=course.id,
            name=name,
            source_relative_path=f"external/{name}",
            status=status,
            found_question_count=counts[0],
            high_confidence_count=counts[1],
            review_count=counts[2],
        )
        image = SourceImage(
            batch=batch,
            source_relative_path=f"{name}.png",
            original_name=f"{name}.png",
            safe_name=f"{name}.png",
            relative_path=f"imports/{name}.png",
            sha256=(name.encode("utf-8").hex() + "0" * 64)[:64],
            mime_type="image/png",
            width=80,
            height=120,
            file_size=1,
            order_index=0,
            ocr_status="completed",
        )
        session.add_all([batch, image])
        session.commit()
        return batch.id, image.id


def test_ordering_api_lists_scores_and_serves_only_owned_local_images(
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
        batch_id, first_id, second_id = seed_ordering_job(app, settings)
        response = client.get(f"/api/import-jobs/{batch_id}/ordering")
        image = client.get(f"/api/import-jobs/{batch_id}/images/{first_id}/content")
        wrong_batch = client.get(f"/api/import-jobs/not-the-batch/images/{first_id}/content")

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["images"]] == [first_id, second_id]
    assert body["overlaps"][0]["combined_score"] == 0.75
    assert body["issues"][0]["code"] == "NEAR_TIE"
    assert body["images"][0]["content_url"].endswith(f"/{first_id}/content")
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert wrong_batch.status_code == 404


def test_ordering_confirmation_requires_every_image_once_and_saves_flags(
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
        batch_id, first_id, second_id = seed_ordering_job(app, settings)
        missing = client.put(
            f"/api/import-jobs/{batch_id}/ordering",
            json={"items": [{"image_id": first_id, "review_flag": None}]},
        )
        repeated = client.put(
            f"/api/import-jobs/{batch_id}/ordering",
            json={"items": [{"image_id": first_id}, {"image_id": first_id}]},
        )
        invalid_flag = client.put(
            f"/api/import-jobs/{batch_id}/ordering",
            json={"items": [{"image_id": first_id, "review_flag": "deleted"}, {"image_id": second_id}]},
        )
        saved = client.put(
            f"/api/import-jobs/{batch_id}/ordering",
            json={"items": [{"image_id": second_id, "review_flag": "unrelated"}, {"image_id": first_id, "review_flag": None}]},
        )

    assert missing.status_code == 400
    assert repeated.status_code == 400
    assert invalid_flag.status_code == 422
    assert saved.status_code == 200
    assert saved.json()["status"] == "ready"
    with app.state.database.session_factory() as session:
        images = list(session.scalars(select(SourceImage).where(SourceImage.batch_id == batch_id).order_by(SourceImage.order_index)))
        issues = list(session.scalars(select(OrderingIssue).where(OrderingIssue.batch_id == batch_id)))
        assert [item.id for item in images] == [second_id, first_id]
        assert images[0].review_flag == "unrelated"
        assert {item.status for item in issues} == {"resolved"}


@pytest.mark.parametrize("status", ["processing", "ready", "question_review"])
def test_ordering_confirmation_rejects_invalid_batch_state_without_mutation(
    tmp_path: Path,
    status: str,
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
        batch_id, first_id, second_id = seed_ordering_job(app, settings)
        with app.state.database.session_factory() as session:
            batch = session.get(ImportBatch, batch_id)
            first = session.get(SourceImage, first_id)
            assert batch is not None and first is not None
            batch.status = status
            first.review_flag = "damaged"
            session.commit()

        response = client.put(
            f"/api/import-jobs/{batch_id}/ordering",
            json={
                "items": [
                    {"image_id": second_id, "review_flag": "unrelated"},
                    {"image_id": first_id, "review_flag": None},
                ]
            },
        )

    assert response.status_code == 409
    assert "当前状态不能确认截图顺序" in response.json()["detail"]

    with app.state.database.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        images = list(
            session.scalars(
                select(SourceImage)
                .where(SourceImage.batch_id == batch_id)
                .order_by(SourceImage.order_index)
            )
        )
        issues = list(
            session.scalars(
                select(OrderingIssue).where(OrderingIssue.batch_id == batch_id)
            )
        )
        assert batch is not None and batch.status == status
        assert [item.id for item in images] == [first_id, second_id]
        assert images[0].review_flag == "damaged"
        assert {item.status for item in issues} == {"pending"}


@pytest.mark.parametrize("review_flag", ["duplicate", "unrelated", "damaged"])
def test_ordering_confirmation_ignores_fully_excluded_candidates_and_recounts(
    tmp_path: Path,
    review_flag: str,
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
        batch_id, first_id, second_id = seed_ordering_job(app, settings)
        excluded_id = seed_candidate(app, batch_id, first_id, part_image_ids=(first_id,))
        kept_id = seed_candidate(app, batch_id, second_id, part_image_ids=(second_id,), stem="Kept question")
        with app.state.database.session_factory() as session:
            batch = session.get(ImportBatch, batch_id)
            assert batch is not None
            batch.found_question_count = 2
            batch.high_confidence_count = 2
            session.commit()

        response = client.put(
            f"/api/import-jobs/{batch_id}/ordering",
            json={"items": [
                {"image_id": first_id, "review_flag": review_flag},
                {"image_id": second_id, "review_flag": None},
            ]},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    with app.state.database.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        assert batch is not None
        assert session.get(QuestionCandidate, excluded_id).status == "ignored"
        assert session.get(QuestionCandidate, kept_id).status == "ready"
        assert (batch.found_question_count, batch.high_confidence_count, batch.review_count) == (1, 1, 0)


def test_ordering_confirmation_moves_mixed_source_candidate_to_review(
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
        batch_id, first_id, second_id = seed_ordering_job(app, settings)
        candidate_id = seed_candidate(
            app,
            batch_id,
            first_id,
            part_image_ids=(first_id, second_id),
        )
        response = client.put(
            f"/api/import-jobs/{batch_id}/ordering",
            json={"items": [
                {"image_id": first_id, "review_flag": "damaged"},
                {"image_id": second_id, "review_flag": None},
            ]},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "question_review"
    with app.state.database.session_factory() as session:
        candidate = session.get(QuestionCandidate, candidate_id)
        batch = session.get(ImportBatch, batch_id)
        assert candidate is not None and batch is not None
        assert candidate.status == "review"
        assert candidate.anomaly_flags_json == [
            {"code": "EXISTING", "severity": "warning", "message": "Preserve"},
            {
                "code": "EXCLUDED_SOURCE_IMAGE",
                "severity": "warning",
                "message": "候选题包含已在顺序检查中排除的来源图片，请检查并调整图片片段",
            },
        ]
        assert (batch.found_question_count, batch.high_confidence_count, batch.review_count) == (1, 0, 1)


def test_ordering_confirmation_reconciles_cross_batch_primary_source(
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
        source_batch_id, excluded_image_id, normal_image_id = seed_ordering_job(
            app, settings
        )
        affected_batch_id, _ = seed_external_batch(
            app,
            name="cross-primary",
            counts=(1, 1, 0),
        )
        candidate_id = seed_candidate(
            app,
            affected_batch_id,
            excluded_image_id,
            stem="Cross-batch primary source",
        )

        response = client.put(
            f"/api/import-jobs/{source_batch_id}/ordering",
            json={
                "items": [
                    {"image_id": excluded_image_id, "review_flag": "damaged"},
                    {"image_id": normal_image_id, "review_flag": None},
                ]
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    with app.state.database.session_factory() as session:
        source_batch = session.get(ImportBatch, source_batch_id)
        affected_batch = session.get(ImportBatch, affected_batch_id)
        candidate = session.get(QuestionCandidate, candidate_id)
        assert source_batch is not None and affected_batch is not None
        assert candidate is not None and candidate.status == "ignored"
        assert (
            source_batch.found_question_count,
            source_batch.high_confidence_count,
            source_batch.review_count,
            source_batch.status,
        ) == (0, 0, 0, "ready")
        assert (
            affected_batch.found_question_count,
            affected_batch.high_confidence_count,
            affected_batch.review_count,
            affected_batch.status,
        ) == (0, 0, 0, "ready")


def test_ordering_confirmation_reconciles_cross_batch_part_source(
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
        source_batch_id, excluded_image_id, normal_image_id = seed_ordering_job(
            app, settings
        )
        affected_batch_id, affected_image_id = seed_external_batch(
            app,
            name="cross-part",
            counts=(1, 1, 0),
        )
        candidate_id = seed_candidate(
            app,
            affected_batch_id,
            affected_image_id,
            part_image_ids=(excluded_image_id,),
            stem="Cross-batch part source",
        )

        response = client.put(
            f"/api/import-jobs/{source_batch_id}/ordering",
            json={
                "items": [
                    {"image_id": excluded_image_id, "review_flag": "unrelated"},
                    {"image_id": normal_image_id, "review_flag": None},
                ]
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    with app.state.database.session_factory() as session:
        source_batch = session.get(ImportBatch, source_batch_id)
        affected_batch = session.get(ImportBatch, affected_batch_id)
        candidate = session.get(QuestionCandidate, candidate_id)
        assert source_batch is not None and affected_batch is not None
        assert candidate is not None and candidate.status == "review"
        assert candidate.anomaly_flags_json == [
            {"code": "EXISTING", "severity": "warning", "message": "Preserve"},
            {
                "code": "EXCLUDED_SOURCE_IMAGE",
                "severity": "warning",
                "message": "候选题包含已在顺序检查中排除的来源图片，请检查并调整图片片段",
            },
        ]
        assert (
            source_batch.found_question_count,
            source_batch.high_confidence_count,
            source_batch.review_count,
            source_batch.status,
        ) == (0, 0, 0, "ready")
        assert (
            affected_batch.found_question_count,
            affected_batch.high_confidence_count,
            affected_batch.review_count,
            affected_batch.status,
        ) == (1, 0, 1, "question_review")


@pytest.mark.parametrize(
    "status",
    ["deleted", "ignored", "duplicate_merged", "merged_source", "replaced_by_ocr"],
)
def test_ordering_confirmation_preserves_inactive_candidate_statuses(
    tmp_path: Path,
    status: str,
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
        batch_id, first_id, second_id = seed_ordering_job(app, settings)
        candidate_id = seed_candidate(app, batch_id, first_id, status=status)
        response = client.put(
            f"/api/import-jobs/{batch_id}/ordering",
            json={"items": [
                {"image_id": first_id, "review_flag": "unrelated"},
                {"image_id": second_id, "review_flag": None},
            ]},
        )

    assert response.status_code == 200
    with app.state.database.session_factory() as session:
        candidate = session.get(QuestionCandidate, candidate_id)
        assert candidate is not None
        assert candidate.status == status


@pytest.mark.parametrize(
    ("excluded_roles", "expected_decision"),
    [
        (("primary",), "kept_secondary"),
        (("secondary",), "kept_primary"),
        (("primary", "secondary"), "source_images_excluded"),
    ],
)
def test_ordering_confirmation_resolves_relationships_for_excluded_candidates(
    tmp_path: Path,
    excluded_roles: tuple[str, ...],
    expected_decision: str,
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
        batch_id, first_id, second_id = seed_ordering_job(app, settings)
        primary_source = first_id if "primary" in excluded_roles else second_id
        secondary_source = first_id if "secondary" in excluded_roles else second_id
        primary_id = seed_candidate(app, batch_id, primary_source, stem="Primary candidate")
        secondary_id = seed_candidate(app, batch_id, secondary_source, stem="Secondary candidate")
        with app.state.database.session_factory() as session:
            relationship = CandidateRelationship(
                batch_id=batch_id,
                primary_candidate_id=primary_id,
                secondary_candidate_id=secondary_id,
                relation_type="cross_screenshot",
                decision="suggested",
                confidence=0.9,
                evidence_json={},
                status="pending",
            )
            session.add(relationship)
            session.commit()
            relationship_id = relationship.id

        response = client.put(
            f"/api/import-jobs/{batch_id}/ordering",
            json={"items": [
                {"image_id": first_id, "review_flag": "unrelated"},
                {"image_id": second_id, "review_flag": None},
            ]},
        )

    assert response.status_code == 200
    with app.state.database.session_factory() as session:
        relationship = session.get(CandidateRelationship, relationship_id)
        assert relationship is not None
        assert relationship.status == "resolved"
        assert relationship.decision == expected_decision


def test_ordering_confirmation_globally_resolves_cross_batch_excluded_relationships(
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
        source_batch_id, excluded_image_id, normal_image_id = seed_ordering_job(
            app, settings
        )
        primary_batch_id, _ = seed_external_batch(
            app,
            name="relationship-primary",
            status="question_review",
            counts=(1, 1, 0),
        )
        secondary_batch_id, _ = seed_external_batch(
            app,
            name="relationship-secondary",
            counts=(1, 1, 0),
        )
        owner_batch_id, _ = seed_external_batch(
            app,
            name="relationship-owner",
            status="question_review",
        )
        primary_id = seed_candidate(
            app,
            primary_batch_id,
            excluded_image_id,
            stem="Cross-batch excluded primary",
        )
        secondary_id = seed_candidate(
            app,
            secondary_batch_id,
            excluded_image_id,
            stem="Cross-batch excluded secondary",
        )
        with app.state.database.session_factory() as session:
            primary_owned = CandidateRelationship(
                batch_id=primary_batch_id,
                primary_candidate_id=primary_id,
                secondary_candidate_id=secondary_id,
                relation_type="cross_screenshot",
                decision="suggested",
                confidence=0.9,
                evidence_json={},
                status="pending",
            )
            third_party_owned = CandidateRelationship(
                batch_id=owner_batch_id,
                primary_candidate_id=primary_id,
                secondary_candidate_id=secondary_id,
                relation_type="possible_duplicate",
                decision="suggested",
                confidence=0.8,
                evidence_json={},
                status="pending",
            )
            session.add_all([primary_owned, third_party_owned])
            session.commit()
            relationship_ids = primary_owned.id, third_party_owned.id

        response = client.put(
            f"/api/import-jobs/{source_batch_id}/ordering",
            json={
                "items": [
                    {"image_id": excluded_image_id, "review_flag": "duplicate"},
                    {"image_id": normal_image_id, "review_flag": None},
                ]
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    with app.state.database.session_factory() as session:
        assert session.get(QuestionCandidate, primary_id).status == "ignored"
        assert session.get(QuestionCandidate, secondary_id).status == "ignored"
        for relationship_id in relationship_ids:
            relationship = session.get(CandidateRelationship, relationship_id)
            assert relationship is not None
            assert (relationship.status, relationship.decision) == (
                "resolved",
                "source_images_excluded",
            )
        for batch_id in (
            source_batch_id,
            primary_batch_id,
            secondary_batch_id,
            owner_batch_id,
        ):
            batch = session.get(ImportBatch, batch_id)
            assert batch is not None
            assert (
                batch.found_question_count,
                batch.high_confidence_count,
                batch.review_count,
                batch.status,
            ) == (0, 0, 0, "ready")


def test_ordering_confirmation_closes_transitive_pending_relationship_graph(
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
        source_batch_id, excluded_image_id, normal_image_id = seed_ordering_job(
            app, settings
        )
        first_batch_id, _ = seed_external_batch(
            app,
            name="transitive-first",
            counts=(1, 1, 0),
        )
        second_batch_id, second_image_id = seed_external_batch(
            app,
            name="transitive-second",
            counts=(1, 1, 0),
        )
        third_batch_id, third_image_id = seed_external_batch(
            app,
            name="transitive-third",
            counts=(1, 1, 0),
        )
        first_owner_batch_id, _ = seed_external_batch(
            app,
            name="transitive-first-owner",
            status="question_review",
        )
        second_owner_batch_id, _ = seed_external_batch(
            app,
            name="transitive-second-owner",
            status="question_review",
        )
        first_id = seed_candidate(
            app,
            first_batch_id,
            excluded_image_id,
            stem="Transitive directly affected",
        )
        second_id = seed_candidate(
            app,
            second_batch_id,
            second_image_id,
            stem="Transitive one-hop excluded",
        )
        third_id = seed_candidate(
            app,
            third_batch_id,
            third_image_id,
            stem="Transitive normal endpoint",
        )
        with app.state.database.session_factory() as session:
            second_image = session.get(SourceImage, second_image_id)
            assert second_image is not None
            second_image.review_flag = "damaged"
            first_relationship = CandidateRelationship(
                batch_id=first_owner_batch_id,
                primary_candidate_id=first_id,
                secondary_candidate_id=second_id,
                relation_type="cross_screenshot",
                decision="suggested",
                confidence=0.9,
                evidence_json={},
                status="pending",
            )
            second_relationship = CandidateRelationship(
                batch_id=second_owner_batch_id,
                primary_candidate_id=second_id,
                secondary_candidate_id=third_id,
                relation_type="possible_duplicate",
                decision="suggested",
                confidence=0.8,
                evidence_json={},
                status="pending",
            )
            session.add_all([first_relationship, second_relationship])
            session.commit()
            relationship_ids = first_relationship.id, second_relationship.id

        response = client.put(
            f"/api/import-jobs/{source_batch_id}/ordering",
            json={
                "items": [
                    {"image_id": excluded_image_id, "review_flag": "unrelated"},
                    {"image_id": normal_image_id, "review_flag": None},
                ]
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    with app.state.database.session_factory() as session:
        first = session.get(QuestionCandidate, first_id)
        second = session.get(QuestionCandidate, second_id)
        third = session.get(QuestionCandidate, third_id)
        assert first is not None and first.status == "ignored"
        assert second is not None and second.status == "ignored"
        assert third is not None and third.status == "ready"
        first_relationship = session.get(CandidateRelationship, relationship_ids[0])
        second_relationship = session.get(CandidateRelationship, relationship_ids[1])
        assert first_relationship is not None and second_relationship is not None
        assert (first_relationship.status, first_relationship.decision) == (
            "resolved",
            "source_images_excluded",
        )
        assert (second_relationship.status, second_relationship.decision) == (
            "resolved",
            "kept_secondary",
        )
        expected_batches = {
            source_batch_id: (0, 0, 0, "ready"),
            first_batch_id: (0, 0, 0, "ready"),
            second_batch_id: (0, 0, 0, "ready"),
            third_batch_id: (1, 1, 0, "ready"),
            first_owner_batch_id: (0, 0, 0, "ready"),
            second_owner_batch_id: (0, 0, 0, "ready"),
        }
        for batch_id, expected in expected_batches.items():
            batch = session.get(ImportBatch, batch_id)
            assert batch is not None
            assert (
                batch.found_question_count,
                batch.high_confidence_count,
                batch.review_count,
                batch.status,
            ) == expected


def test_ordering_confirmation_rolls_back_everything_when_recount_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    with TestClient(app, raise_server_exceptions=False) as client:
        batch_id, first_id, second_id = seed_ordering_job(app, settings)
        candidate_id = seed_candidate(app, batch_id, first_id)
        kept_id = seed_candidate(app, batch_id, second_id, stem="Kept after rollback")
        with app.state.database.session_factory() as session:
            batch = session.get(ImportBatch, batch_id)
            assert batch is not None
            batch.found_question_count = 2
            batch.high_confidence_count = 2
            relationship = CandidateRelationship(
                batch_id=batch_id,
                primary_candidate_id=candidate_id,
                secondary_candidate_id=kept_id,
                relation_type="cross_screenshot",
                decision="suggested",
                confidence=0.9,
                evidence_json={},
                status="pending",
            )
            session.add(relationship)
            session.commit()
            relationship_id = relationship.id

        def fail_recount(session, requested_batch_id):
            assert requested_batch_id == batch_id
            raise RuntimeError("recount failed")

        monkeypatch.setattr(ordering_exclusions, "update_batch_review_state", fail_recount)
        response = client.put(
            f"/api/import-jobs/{batch_id}/ordering",
            json={"items": [
                {"image_id": second_id, "review_flag": None},
                {"image_id": first_id, "review_flag": "damaged"},
            ]},
        )

    assert response.status_code == 500
    with app.state.database.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        images = list(
            session.scalars(
                select(SourceImage)
                .where(SourceImage.batch_id == batch_id)
                .order_by(SourceImage.order_index)
            )
        )
        issues = list(
            session.scalars(
                select(OrderingIssue).where(OrderingIssue.batch_id == batch_id)
            )
        )
        assert batch is not None
        assert batch.status == "ordering_review"
        assert (
            batch.found_question_count,
            batch.high_confidence_count,
            batch.review_count,
        ) == (2, 2, 0)
        assert [image.id for image in images] == [first_id, second_id]
        assert [image.review_flag for image in images] == [None, None]
        candidate = session.get(QuestionCandidate, candidate_id)
        assert candidate is not None
        assert candidate.status == "ready"
        assert candidate.anomaly_flags_json == [
            {"code": "EXISTING", "severity": "warning", "message": "Preserve"}
        ]
        relationship = session.get(CandidateRelationship, relationship_id)
        assert relationship is not None
        assert (relationship.status, relationship.decision) == (
            "pending",
            "suggested",
        )
        assert {issue.status for issue in issues} == {"pending"}


def test_ordering_confirmation_preserves_reviewable_and_unrelated_relationships(
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
        batch_id, first_id, second_id = seed_ordering_job(app, settings)
        mixed_id = seed_candidate(
            app,
            batch_id,
            first_id,
            part_image_ids=(first_id, second_id),
            stem="Mixed source",
        )
        normal_one_id = seed_candidate(app, batch_id, second_id, stem="Normal one")
        normal_two_id = seed_candidate(app, batch_id, second_id, stem="Normal two")
        with app.state.database.session_factory() as session:
            mixed_relationship = CandidateRelationship(
                batch_id=batch_id,
                primary_candidate_id=mixed_id,
                secondary_candidate_id=normal_one_id,
                relation_type="cross_screenshot",
                decision="suggested",
                confidence=0.9,
                evidence_json={},
                status="pending",
            )
            unrelated_relationship = CandidateRelationship(
                batch_id=batch_id,
                primary_candidate_id=normal_one_id,
                secondary_candidate_id=normal_two_id,
                relation_type="possible_duplicate",
                decision="suggested",
                confidence=0.8,
                evidence_json={},
                status="pending",
            )
            session.add_all([mixed_relationship, unrelated_relationship])
            session.commit()
            mixed_relationship_id = mixed_relationship.id
            unrelated_relationship_id = unrelated_relationship.id

        response = client.put(
            f"/api/import-jobs/{batch_id}/ordering",
            json={"items": [
                {"image_id": first_id, "review_flag": "unrelated"},
                {"image_id": second_id, "review_flag": None},
            ]},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "question_review"
    with app.state.database.session_factory() as session:
        mixed_relationship = session.get(CandidateRelationship, mixed_relationship_id)
        unrelated_relationship = session.get(
            CandidateRelationship, unrelated_relationship_id
        )
        assert mixed_relationship is not None
        assert unrelated_relationship is not None
        assert mixed_relationship.status == "pending"
        assert mixed_relationship.decision == "suggested"
        assert unrelated_relationship.status == "pending"
        assert unrelated_relationship.decision == "suggested"
