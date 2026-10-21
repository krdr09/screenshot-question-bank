import logging
import os
from pathlib import Path

import pytest
from PIL import Image

from app.config import Settings
from app.cropping.questions import CropBounds
from app.cropping.questions import crop_question_image as real_crop_question_image
from app.database import Database
from app.models import CandidateImagePart, CandidateRelationship, Course, ImportBatch, QuestionCandidate, ReviewAction, SourceImage
from app.services.review import crop_files, editor
from app.services.review.editor import (
    ReviewValidationError,
    confirm_candidate,
    delete_candidate,
    ignore_candidate,
    undo_last_action,
    update_candidate,
)


def _seed(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing",
        _env_file=None,
    )
    database = Database(settings)
    database.create_schema()
    source_path = settings.data_dir / "imports/source.png"
    source_path.parent.mkdir(parents=True)
    Image.new("RGB", (120, 240), "white").save(source_path)
    crop_path = settings.data_dir / "crops/original.png"
    crop_path.parent.mkdir(parents=True)
    Image.new("RGB", (120, 100), "white").save(crop_path)
    with database.session_factory() as session:
        course = Course(name="课程")
        batch = ImportBatch(
            course=course,
            name="批次",
            source_relative_path="课程/批次",
            status="question_review",
            found_question_count=1,
            review_count=1,
        )
        image = SourceImage(
            batch=batch,
            source_relative_path="source.png",
            original_name="source.png",
            safe_name="source.png",
            relative_path="imports/source.png",
            sha256="a" * 64,
            mime_type="image/png",
            width=120,
            height=240,
            file_size=source_path.stat().st_size,
            order_index=0,
            ocr_status="completed",
        )
        session.add_all([batch, image])
        session.flush()
        candidate = QuestionCandidate(
            batch_id=batch.id,
            source_image=image,
            question_type="single_choice",
            stem="旧题干",
            options_json=[{"label": "A", "text": "甲"}, {"label": "B", "text": "乙"}],
            correct_answer_json=None,
            raw_ocr_text="原始 OCR",
            normalized_ocr_text="原始 OCR",
            ocr_confidence=0.7,
            parse_confidence=0.6,
            display_mode="text_first",
            anomaly_flags_json=[{"code": "MISSING_ANSWER", "severity": "error", "message": "缺答案"}],
            status="review",
        )
        session.add(candidate)
        session.flush()
        session.add(
            CandidateImagePart(
                candidate_id=candidate.id,
                source_image_id=image.id,
                crop_path="crops/original.png",
                source_y_start=20,
                source_y_end=120,
                part_index=0,
            )
        )
        session.commit()
        return database, settings, candidate.id, batch.id


def _add_second_part(
    database: Database,
    settings: Settings,
    candidate_id: str,
) -> tuple[str, str]:
    second_crop_path = settings.data_dir / "crops/second.png"
    Image.new("RGB", (120, 100), "navy").save(second_crop_path)
    with database.session_factory() as session:
        candidate = session.get(QuestionCandidate, candidate_id)
        first_part = candidate.image_parts[0]
        second_part = CandidateImagePart(
            candidate_id=candidate.id,
            source_image_id=first_part.source_image_id,
            crop_path="crops/second.png",
            source_y_start=130,
            source_y_end=230,
            part_index=1,
        )
        session.add(second_part)
        session.commit()
        return first_part.id, second_part.id


def _update_two_parts_to_120(
    database: Database,
    settings: Settings,
    candidate_id: str,
    first_id: str,
    second_id: str,
) -> tuple[Path, Path, bytes, bytes]:
    with database.session_factory() as session:
        update_candidate(
            session,
            settings,
            candidate_id,
            {
                "correct_answer": "A",
                "parts": [
                    {
                        "id": first_id,
                        "source_y_start": 20,
                        "source_y_end": 140,
                    },
                    {
                        "id": second_id,
                        "source_y_start": 120,
                        "source_y_end": 240,
                    },
                ],
            },
        )
    first_crop = settings.data_dir / "crops/original.png"
    second_crop = settings.data_dir / "crops/second.png"
    return first_crop, second_crop, first_crop.read_bytes(), second_crop.read_bytes()


def _seed_relationship(
    tmp_path: Path,
) -> tuple[Database, Settings, str, str, str]:
    database, settings, primary_id, _ = _seed(tmp_path)
    secondary_crop = settings.data_dir / "crops/relationship-secondary.png"
    Image.new("RGB", (120, 100), "navy").save(secondary_crop)
    with database.session_factory() as session:
        primary = session.get(QuestionCandidate, primary_id)
        primary_part = primary.image_parts[0]
        secondary = QuestionCandidate(
            batch_id=primary.batch_id,
            source_image_id=primary.source_image_id,
            question_type="single_choice",
            stem="第二题",
            options_json=[
                {"label": "A", "text": "甲"},
                {"label": "B", "text": "乙"},
            ],
            correct_answer_json="A",
            raw_ocr_text="第二题 OCR",
            normalized_ocr_text="第二题 OCR",
            ocr_confidence=0.8,
            parse_confidence=0.8,
            display_mode="text",
            anomaly_flags_json=[],
            status="review",
        )
        session.add(secondary)
        session.flush()
        session.add(
            CandidateImagePart(
                candidate_id=secondary.id,
                source_image_id=primary_part.source_image_id,
                crop_path="crops/relationship-secondary.png",
                source_y_start=20,
                source_y_end=120,
                part_index=0,
            )
        )
        relationship = CandidateRelationship(
            batch_id=primary.batch_id,
            primary_candidate_id=primary.id,
            secondary_candidate_id=secondary.id,
            relation_type="cross_screenshot",
            decision="suggested",
            confidence=0.9,
            evidence_json={},
            status="pending",
        )
        session.add(relationship)
        session.commit()
        return database, settings, primary.id, secondary.id, relationship.id


def _crop_bytes(
    database: Database,
    settings: Settings,
    candidate_id: str,
) -> bytes:
    with database.session_factory() as session:
        candidate = session.get(QuestionCandidate, candidate_id)
        crop_path = candidate.image_parts[0].crop_path
    return (settings.data_dir / crop_path).read_bytes()


def test_update_candidate_validates_structure_updates_crop_and_can_undo(tmp_path: Path) -> None:
    database, settings, candidate_id, _ = _seed(tmp_path)
    with database.session_factory() as session:
        with pytest.raises(ReviewValidationError, match="连续"):
            update_candidate(
                session,
                settings,
                candidate_id,
                {"options": [{"label": "A", "text": "甲"}, {"label": "C", "text": "丙"}], "correct_answer": "A"},
            )
        updated = update_candidate(
            session,
            settings,
            candidate_id,
            {
                "edited_ocr_text": "编辑 OCR",
                "stem": "新题干",
                "options": [{"label": "A", "text": "新甲"}, {"label": "B", "text": "新乙"}],
                "correct_answer": "B",
                "explanation": "新解析",
                "display_mode": "image_first",
                "parts": [{"id": None, "part_index": 0, "source_y_start": 30, "source_y_end": 150}],
            },
        )
        assert updated.stem == "新题干"
        assert updated.edited_ocr_text == "编辑 OCR"
        assert updated.image_parts[0].source_y_start == 30
        with Image.open(settings.data_dir / updated.image_parts[0].crop_path) as crop:
            assert crop.size == (120, 120)
        assert session.query(ReviewAction).count() == 1

        restored = undo_last_action(session, settings, candidate_id)
        assert restored.stem == "旧题干"
        assert restored.edited_ocr_text is None
        assert restored.image_parts[0].source_y_start == 20


def test_candidate_state_actions_recalculate_batch_and_validate_true_false(tmp_path: Path) -> None:
    database, settings, candidate_id, batch_id = _seed(tmp_path)
    with database.session_factory() as session:
        with pytest.raises(ReviewValidationError, match="判断题"):
            update_candidate(
                session,
                settings,
                candidate_id,
                {"question_type": "true_false", "correct_answer": True},
            )
        update_candidate(
            session,
            settings,
            candidate_id,
            {
                "question_type": "true_false",
                "options": [{"label": "A", "text": "错误"}, {"label": "B", "text": "正确"}],
                "correct_answer": True,
                "true_label": "B",
                "false_label": "A",
            },
        )
        confirmed = confirm_candidate(session, candidate_id)
        assert confirmed.status == "ready"
        batch = session.get(ImportBatch, batch_id)
        assert batch.review_count == 0 and batch.high_confidence_count == 1
        assert batch.status == "ready"

        ignored = ignore_candidate(session, candidate_id)
        assert ignored.status == "ignored"
        deleted = delete_candidate(session, candidate_id)
        assert deleted.status == "deleted"


def test_stage_four_display_modes_remain_editable(tmp_path: Path) -> None:
    database, settings, candidate_id, _ = _seed(tmp_path)
    with database.session_factory() as session:
        candidate = session.get(QuestionCandidate, candidate_id)
        candidate.display_mode = "text"
        session.commit()

        updated = update_candidate(
            session,
            settings,
            candidate_id,
            {"correct_answer": "A"},
        )

    assert updated.display_mode == "text"


def test_multi_part_recrop_validates_every_part_before_changing_files(tmp_path: Path) -> None:
    database, settings, candidate_id, _ = _seed(tmp_path)
    first_id, second_id = _add_second_part(database, settings, candidate_id)
    first_crop = settings.data_dir / "crops/original.png"
    original_bytes = first_crop.read_bytes()

    with database.session_factory() as session:
        with pytest.raises(ReviewValidationError, match="裁切范围"):
            update_candidate(
                session,
                settings,
                candidate_id,
                {
                    "correct_answer": "A",
                    "parts": [
                        {
                            "id": first_id,
                            "source_y_start": 30,
                            "source_y_end": 160,
                        },
                        {
                            "id": second_id,
                            "source_y_start": 180,
                            "source_y_end": 260,
                        },
                    ],
                },
            )
        session.rollback()

    with database.session_factory() as session:
        first_part = session.get(CandidateImagePart, first_id)
        second_part = session.get(CandidateImagePart, second_id)
        assert (first_part.source_y_start, first_part.source_y_end) == (20, 120)
        assert (second_part.source_y_start, second_part.source_y_end) == (130, 230)
    assert first_crop.read_bytes() == original_bytes
    assert not list(first_crop.parent.glob(".*.recrop-*"))


def test_relationship_recrop_validates_both_candidates_before_changes(
    tmp_path: Path,
) -> None:
    database, settings, primary_id, secondary_id, relationship_id = (
        _seed_relationship(tmp_path)
    )
    primary_crop = _crop_bytes(database, settings, primary_id)
    secondary_crop = _crop_bytes(database, settings, secondary_id)

    with database.session_factory() as session:
        with pytest.raises(ReviewValidationError, match="裁切范围"):
            editor.update_relationship_crops(
                session,
                settings,
                relationship_id,
                [{"part_index": 0, "source_y_start": 30, "source_y_end": 110}],
                [{"part_index": 0, "source_y_start": 40, "source_y_end": 999}],
            )

    with database.session_factory() as session:
        primary = session.get(QuestionCandidate, primary_id)
        secondary = session.get(QuestionCandidate, secondary_id)
        assert (
            primary.image_parts[0].source_y_start,
            primary.image_parts[0].source_y_end,
        ) == (20, 120)
        assert (
            secondary.image_parts[0].source_y_start,
            secondary.image_parts[0].source_y_end,
        ) == (20, 120)
    assert _crop_bytes(database, settings, primary_id) == primary_crop
    assert _crop_bytes(database, settings, secondary_id) == secondary_crop


def test_relationship_recrop_commits_both_candidates_and_audits(
    tmp_path: Path,
) -> None:
    database, settings, primary_id, secondary_id, relationship_id = (
        _seed_relationship(tmp_path)
    )

    with database.session_factory() as session:
        primary, secondary = editor.update_relationship_crops(
            session,
            settings,
            relationship_id,
            [{"part_index": 0, "source_y_start": 30, "source_y_end": 110}],
            [{"part_index": 0, "source_y_start": 40, "source_y_end": 130}],
        )

        assert (
            primary.image_parts[0].source_y_start,
            primary.image_parts[0].source_y_end,
        ) == (30, 110)
        assert (
            secondary.image_parts[0].source_y_start,
            secondary.image_parts[0].source_y_end,
        ) == (40, 130)
        assert (
            session.query(ReviewAction)
            .filter(
                ReviewAction.candidate_id == primary_id,
                ReviewAction.action_type == "update",
            )
            .count()
            == 1
        )
        assert (
            session.query(ReviewAction)
            .filter(
                ReviewAction.candidate_id == secondary_id,
                ReviewAction.action_type == "update",
            )
            .count()
            == 1
        )

    with database.session_factory() as session:
        primary = session.get(QuestionCandidate, primary_id)
        secondary = session.get(QuestionCandidate, secondary_id)
        with Image.open(settings.data_dir / primary.image_parts[0].crop_path) as crop:
            assert crop.size == (120, 80)
        with Image.open(settings.data_dir / secondary.image_parts[0].crop_path) as crop:
            assert crop.size == (120, 90)


def test_relationship_recrop_install_failure_restores_both_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, settings, primary_id, secondary_id, relationship_id = (
        _seed_relationship(tmp_path)
    )
    primary_crop = _crop_bytes(database, settings, primary_id)
    secondary_crop = _crop_bytes(database, settings, secondary_id)
    real_replace = os.replace
    staged_install_count = 0

    def fail_second_staged_install(source, target) -> None:
        nonlocal staged_install_count
        if ".recrop-stage-" in Path(source).name:
            staged_install_count += 1
            if staged_install_count == 2:
                raise OSError("synthetic relationship install failure")
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_second_staged_install)

    with database.session_factory() as session:
        with pytest.raises(
            OSError,
            match="synthetic relationship install failure",
        ):
            editor.update_relationship_crops(
                session,
                settings,
                relationship_id,
                [{"part_index": 0, "source_y_start": 30, "source_y_end": 110}],
                [{"part_index": 0, "source_y_start": 40, "source_y_end": 130}],
            )

    with database.session_factory() as session:
        primary = session.get(QuestionCandidate, primary_id)
        secondary = session.get(QuestionCandidate, secondary_id)
        assert (
            primary.image_parts[0].source_y_start,
            primary.image_parts[0].source_y_end,
        ) == (20, 120)
        assert (
            secondary.image_parts[0].source_y_start,
            secondary.image_parts[0].source_y_end,
        ) == (20, 120)
        assert (
            session.query(ReviewAction)
            .filter(ReviewAction.candidate_id.in_([primary_id, secondary_id]))
            .count()
            == 0
        )
    assert _crop_bytes(database, settings, primary_id) == primary_crop
    assert _crop_bytes(database, settings, secondary_id) == secondary_crop
    assert not list((settings.data_dir / "crops").rglob(".*.recrop-*"))


def test_multi_part_recrop_rolls_back_files_when_second_install_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, settings, candidate_id, _ = _seed(tmp_path)
    first_id, second_id = _add_second_part(database, settings, candidate_id)
    first_crop = settings.data_dir / "crops/original.png"
    second_crop = settings.data_dir / "crops/second.png"
    original_first = first_crop.read_bytes()
    original_second = second_crop.read_bytes()
    real_replace = os.replace
    staged_install_count = 0

    def fail_second_staged_install(source, target) -> None:
        nonlocal staged_install_count
        if ".recrop-stage-" in Path(source).name:
            staged_install_count += 1
            if staged_install_count == 2:
                raise OSError("synthetic second install failure")
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_second_staged_install)

    with database.session_factory() as session:
        with pytest.raises(OSError, match="synthetic second install failure"):
            update_candidate(
                session,
                settings,
                candidate_id,
                {
                    "correct_answer": "A",
                    "parts": [
                        {
                            "id": first_id,
                            "source_y_start": 30,
                            "source_y_end": 160,
                        },
                        {
                            "id": second_id,
                            "source_y_start": 140,
                            "source_y_end": 220,
                        },
                    ],
                },
            )

    with database.session_factory() as session:
        first_part = session.get(CandidateImagePart, first_id)
        second_part = session.get(CandidateImagePart, second_id)
        candidate = session.get(QuestionCandidate, candidate_id)
        assert (first_part.source_y_start, first_part.source_y_end) == (20, 120)
        assert (second_part.source_y_start, second_part.source_y_end) == (130, 230)
        assert candidate.correct_answer_json is None
    assert first_crop.read_bytes() == original_first
    assert second_crop.read_bytes() == original_second
    assert not list(first_crop.parent.glob(".*.recrop-*"))


def test_multi_part_undo_stage_failure_preserves_files_and_remains_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, settings, candidate_id, batch_id = _seed(tmp_path)
    first_id, second_id = _add_second_part(database, settings, candidate_id)
    first_crop, second_crop, updated_first, updated_second = _update_two_parts_to_120(
        database,
        settings,
        candidate_id,
        first_id,
        second_id,
    )
    crop_call_count = 0

    def fail_second_crop(source, target, bounds) -> None:
        nonlocal crop_call_count
        crop_call_count += 1
        if crop_call_count == 2:
            raise OSError("synthetic undo second crop failure")
        real_crop_question_image(source, target, bounds)

    monkeypatch.setattr(editor, "crop_question_image", fail_second_crop, raising=False)
    monkeypatch.setattr(crop_files, "crop_question_image", fail_second_crop)

    with database.session_factory() as session:
        with pytest.raises(OSError, match="synthetic undo second crop failure"):
            undo_last_action(session, settings, candidate_id)

    with database.session_factory() as session:
        first_part = session.get(CandidateImagePart, first_id)
        second_part = session.get(CandidateImagePart, second_id)
        candidate = session.get(QuestionCandidate, candidate_id)
        batch = session.get(ImportBatch, batch_id)
        action = (
            session.query(ReviewAction)
            .filter(ReviewAction.candidate_id == candidate_id)
            .one()
        )
        assert (first_part.source_y_start, first_part.source_y_end) == (20, 140)
        assert (second_part.source_y_start, second_part.source_y_end) == (120, 240)
        assert candidate.correct_answer_json == "A"
        assert action.undone_at is None
        assert batch.status == "question_review"
        assert batch.found_question_count == 1
        assert batch.high_confidence_count == 0
        assert batch.review_count == 1
    assert first_crop.read_bytes() == updated_first
    assert second_crop.read_bytes() == updated_second
    assert not list((settings.data_dir / "crops").rglob(".*.recrop-*"))
    assert not list((settings.data_dir / "crops").rglob(".*.crop-*.tmp"))

    monkeypatch.undo()
    with database.session_factory() as session:
        restored = undo_last_action(session, settings, candidate_id)
        assert [
            (part.source_y_start, part.source_y_end)
            for part in sorted(restored.image_parts, key=lambda item: item.part_index)
        ] == [(20, 120), (130, 230)]
        assert restored.correct_answer_json is None
        batch = session.get(ImportBatch, batch_id)
        action = (
            session.query(ReviewAction)
            .filter(ReviewAction.candidate_id == candidate_id)
            .one()
        )
        assert action.undone_at is not None
        assert batch.status == "question_review"
        assert batch.found_question_count == 1
        assert batch.high_confidence_count == 0
        assert batch.review_count == 1
    with Image.open(first_crop) as image:
        assert image.size == (120, 100)
    with Image.open(second_crop) as image:
        assert image.size == (120, 100)
    assert not list((settings.data_dir / "crops").rglob(".*.recrop-*"))


def test_multi_part_undo_second_install_failure_restores_everything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, settings, candidate_id, batch_id = _seed(tmp_path)
    first_id, second_id = _add_second_part(database, settings, candidate_id)
    first_crop, second_crop, updated_first, updated_second = _update_two_parts_to_120(
        database,
        settings,
        candidate_id,
        first_id,
        second_id,
    )
    real_replace = os.replace
    staged_install_count = 0

    def fail_second_staged_install(source, target) -> None:
        nonlocal staged_install_count
        source_name = Path(source).name
        if ".recrop-stage-" in source_name and ".crop-" not in source_name:
            staged_install_count += 1
            if staged_install_count == 2:
                raise OSError("synthetic undo second install failure")
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_second_staged_install)

    with database.session_factory() as session:
        with pytest.raises(OSError, match="synthetic undo second install failure"):
            undo_last_action(session, settings, candidate_id)

    with database.session_factory() as session:
        first_part = session.get(CandidateImagePart, first_id)
        second_part = session.get(CandidateImagePart, second_id)
        candidate = session.get(QuestionCandidate, candidate_id)
        batch = session.get(ImportBatch, batch_id)
        action = (
            session.query(ReviewAction)
            .filter(ReviewAction.candidate_id == candidate_id)
            .one()
        )
        assert (first_part.source_y_start, first_part.source_y_end) == (20, 140)
        assert (second_part.source_y_start, second_part.source_y_end) == (120, 240)
        assert candidate.correct_answer_json == "A"
        assert action.undone_at is None
        assert batch.status == "question_review"
        assert batch.found_question_count == 1
        assert batch.high_confidence_count == 0
        assert batch.review_count == 1
    assert staged_install_count == 2
    assert first_crop.read_bytes() == updated_first
    assert second_crop.read_bytes() == updated_second
    assert not list((settings.data_dir / "crops").rglob(".*.recrop-*"))
    assert not list((settings.data_dir / "crops").rglob(".*.crop-*.tmp"))


def test_undo_finalize_cleanup_failure_keeps_success_and_does_not_retry_older_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database, settings, candidate_id, batch_id = _seed(tmp_path)
    first_id, second_id = _add_second_part(database, settings, candidate_id)
    with database.session_factory() as session:
        update_candidate(
            session,
            settings,
            candidate_id,
            {
                "correct_answer": "A",
                "parts": [
                    {
                        "id": first_id,
                        "source_y_start": 20,
                        "source_y_end": 130,
                    },
                    {
                        "id": second_id,
                        "source_y_start": 125,
                        "source_y_end": 235,
                    },
                ],
            },
        )
        older_action_id = (
            session.query(ReviewAction)
            .filter(ReviewAction.candidate_id == candidate_id)
            .one()
            .id
        )
    first_crop, second_crop, _, _ = _update_two_parts_to_120(
        database,
        settings,
        candidate_id,
        first_id,
        second_id,
    )
    with database.session_factory() as session:
        latest_action_id = (
            session.query(ReviewAction)
            .filter(
                ReviewAction.candidate_id == candidate_id,
                ReviewAction.id != older_action_id,
            )
            .one()
            .id
        )

    real_unlink = Path.unlink
    backup_cleanup_attempts: list[Path] = []
    injected = False

    def fail_first_backup_cleanup(path: Path, *args, **kwargs) -> None:
        nonlocal injected
        if ".recrop-backup-" in path.name:
            backup_cleanup_attempts.append(path)
            if not injected:
                injected = True
                raise OSError("synthetic post-commit backup cleanup failure")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_first_backup_cleanup)
    undo_attempts = 0
    caplog.set_level(logging.WARNING)
    for _ in range(2):
        undo_attempts += 1
        try:
            with database.session_factory() as session:
                undo_last_action(session, settings, candidate_id)
        except OSError:
            continue
        break

    assert undo_attempts == 1
    assert len(backup_cleanup_attempts) == 2
    assert "synthetic post-commit backup cleanup failure" in caplog.text
    with database.session_factory() as session:
        candidate = session.get(QuestionCandidate, candidate_id)
        batch = session.get(ImportBatch, batch_id)
        older_action = session.get(ReviewAction, older_action_id)
        latest_action = session.get(ReviewAction, latest_action_id)
        assert candidate.correct_answer_json == "A"
        assert [
            (part.source_y_start, part.source_y_end)
            for part in sorted(candidate.image_parts, key=lambda item: item.part_index)
        ] == [(20, 130), (125, 235)]
        assert latest_action.undone_at is not None
        assert older_action.undone_at is None
        assert batch.status == "question_review"
        assert batch.found_question_count == 1
        assert batch.high_confidence_count == 0
        assert batch.review_count == 1
    with Image.open(first_crop) as image:
        assert image.size == (120, 110)
    with Image.open(second_crop) as image:
        assert image.size == (120, 110)
    for path in backup_cleanup_attempts:
        real_unlink(path, missing_ok=True)
    assert not list(first_crop.parent.glob(".*.recrop-*"))


@pytest.mark.parametrize(
    "target_length",
    [
        pytest.param(212, id="backup-name-reaches-limit"),
        pytest.param(213, id="stage-name-reaches-limit"),
    ],
)
def test_crop_transaction_shortens_generated_paths_at_windows_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_length: int,
) -> None:
    target_name = "crop.png"
    padding = (
        target_length
        - len(os.path.abspath(tmp_path))
        - (2 * len(os.sep))
        - len(target_name)
    )
    assert padding > 0
    directory = tmp_path / ("x" * padding)
    directory.mkdir()
    source = directory / "source.png"
    target = directory / target_name
    Image.new("RGB", (4, 2), "red").save(source)
    Image.new("RGB", (4, 2), "white").save(target)
    assert len(os.path.abspath(target)) == target_length

    real_replace = os.replace

    def windows_legacy_replace(source_path, target_path) -> None:
        if max(
            len(os.path.abspath(source_path)),
            len(os.path.abspath(target_path)),
        ) >= 260:
            raise FileNotFoundError(
                3,
                "synthetic Windows MAX_PATH boundary",
                str(target_path),
            )
        real_replace(source_path, target_path)

    monkeypatch.setattr(os, "replace", windows_legacy_replace)
    transaction = crop_files.CropFileTransaction.stage(
        [
            crop_files.CropReplacement(
                source=source,
                target=target,
                bounds=CropBounds(y_start=0, y_end=1, page_height=2),
            )
        ]
    )
    transaction.install()
    transaction.finalize()

    with Image.open(target) as image:
        assert image.size == (4, 1)
        assert image.getpixel((0, 0)) == (255, 0, 0)
    assert not [path for path in directory.iterdir() if ".recrop-" in path.name]


def test_stage_cleanup_failure_keeps_primary_error_and_attempts_every_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    primary_error = OSError("synthetic primary stage failure")
    cleanup_error = OSError("synthetic secondary stage cleanup failure")
    crop_calls = 0

    def fail_third_crop(source: Path, target: Path, bounds: CropBounds) -> None:
        nonlocal crop_calls
        crop_calls += 1
        target.write_bytes(f"staged-{crop_calls}".encode())
        if crop_calls == 3:
            raise primary_error

    real_unlink = Path.unlink
    cleanup_attempts: list[Path] = []
    cleanup_failed = False

    def fail_first_stage_cleanup(path: Path, *args, **kwargs) -> None:
        nonlocal cleanup_failed
        if ".recrop-stage-" in path.name:
            cleanup_attempts.append(path)
            if not cleanup_failed:
                cleanup_failed = True
                raise cleanup_error
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(crop_files, "crop_question_image", fail_third_crop)
    monkeypatch.setattr(Path, "unlink", fail_first_stage_cleanup)
    replacements = [
        crop_files.CropReplacement(
            source=tmp_path / "source.png",
            target=tmp_path / f"target-{index}.png",
            bounds=CropBounds(y_start=0, y_end=1, page_height=1),
        )
        for index in range(3)
    ]

    caplog.set_level(logging.WARNING)
    with pytest.raises(OSError) as exc_info:
        crop_files.CropFileTransaction.stage(replacements)

    assert exc_info.value is primary_error
    assert len(cleanup_attempts) == 3
    assert "synthetic secondary stage cleanup failure" in caplog.text
    for path in cleanup_attempts:
        real_unlink(path, missing_ok=True)
    assert not list(tmp_path.glob(".*.recrop-*"))


def test_undo_compensation_failures_keep_commit_error_and_continue_file_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database, settings, candidate_id, batch_id = _seed(tmp_path)
    first_id, second_id = _add_second_part(database, settings, candidate_id)
    first_crop, second_crop, updated_first, updated_second = _update_two_parts_to_120(
        database,
        settings,
        candidate_id,
        first_id,
        second_id,
    )
    primary_error = RuntimeError("synthetic primary commit failure")
    database_rollback_error = OSError("synthetic database rollback failure")
    file_restore_error = OSError("synthetic secondary file restore failure")
    real_replace = os.replace
    restore_attempts: list[Path] = []
    restore_failed = False

    def fail_second_backup_restore(source, target) -> None:
        nonlocal restore_failed
        source_path = Path(source)
        target_path = Path(target)
        if ".recrop-backup-" in source_path.name:
            restore_attempts.append(target_path)
            if target_path == second_crop and not restore_failed:
                restore_failed = True
                raise file_restore_error
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_second_backup_restore)
    caplog.set_level(logging.WARNING)
    rollback_attempts = 0
    with database.session_factory() as session:
        def fail_commit() -> None:
            raise primary_error

        def fail_database_rollback() -> None:
            nonlocal rollback_attempts
            rollback_attempts += 1
            raise database_rollback_error

        monkeypatch.setattr(session, "commit", fail_commit)
        monkeypatch.setattr(session, "rollback", fail_database_rollback)
        with pytest.raises(RuntimeError) as exc_info:
            undo_last_action(session, settings, candidate_id)

    assert exc_info.value is primary_error
    assert rollback_attempts == 1
    assert restore_attempts == [second_crop, first_crop]
    assert first_crop.read_bytes() == updated_first
    assert second_crop.is_file()
    assert second_crop.read_bytes() != updated_second
    assert "synthetic database rollback failure" in caplog.text
    assert "synthetic secondary file restore failure" in caplog.text
    with database.session_factory() as session:
        candidate = session.get(QuestionCandidate, candidate_id)
        batch = session.get(ImportBatch, batch_id)
        action = (
            session.query(ReviewAction)
            .filter(ReviewAction.candidate_id == candidate_id)
            .one()
        )
        assert candidate.correct_answer_json == "A"
        assert [
            (part.source_y_start, part.source_y_end)
            for part in sorted(candidate.image_parts, key=lambda item: item.part_index)
        ] == [(20, 140), (120, 240)]
        assert action.undone_at is None
        assert batch.status == "question_review"
        assert batch.found_question_count == 1
        assert batch.high_confidence_count == 0
        assert batch.review_count == 1
    failed_backup = next(second_crop.parent.glob(".*.recrop-backup-*"))
    real_replace(failed_backup, second_crop)
    assert not list(second_crop.parent.glob(".*.recrop-*"))
