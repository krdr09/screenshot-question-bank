import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.database import Database
from app.main import create_app
from app.models import (
    CandidateImagePart,
    Category,
    Course,
    ImportBatch,
    OrderingIssue,
    PracticeItem,
    PracticeSession,
    QuestionCandidate,
    ReviewAction,
    ScreenshotOverlap,
    SourceImage,
    WrongBookEntry,
)
from app.ocr.mock import MockOcrEngine
from app.services.library.batch_delete import LibraryDeletionError, delete_import_batch
from app.services.library.deletion_recovery import (
    BatchDeletionJournal,
    BatchDeletionRecoveryError,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing",
        _env_file=None,
    )


def _candidate(batch, image, category, stem):
    return QuestionCandidate(
        batch_id=batch.id,
        category_id=category.id,
        source_image=image,
        question_type="single_choice",
        stem=stem,
        options_json=[{"label": "A", "text": "甲"}, {"label": "B", "text": "乙"}],
        correct_answer_json="A",
        raw_ocr_text=stem,
        normalized_ocr_text=stem,
        ocr_confidence=0.99,
        parse_confidence=0.99,
        display_mode="text",
        anomaly_flags_json=[],
        status="ready",
    )


def _database_and_seed(tmp_path: Path):
    settings = _settings(tmp_path)
    database = Database(settings)
    database.create_schema()
    user_original = tmp_path / "user-originals" / "不要删除.png"
    user_original.parent.mkdir(parents=True)
    Image.new("RGB", (50, 50), "red").save(user_original)
    shared = settings.data_dir / "imports/shared.png"
    unique = settings.data_dir / "imports/unique.png"
    crop = settings.data_dir / "crops/delete-me.png"
    for path, color in ((shared, "white"), (unique, "blue"), (crop, "green")):
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (50, 50), color).save(path)
    with database.session_factory() as session:
        course = Course(name="医学")
        category = Category(course=course, path="内科")
        target = ImportBatch(course=course, category=category, name="待删批次", source_relative_path="用户原目录/待删")
        survivor = ImportBatch(course=course, category=category, name="保留批次", source_relative_path="用户原目录/保留")
        session.add_all([target, survivor])
        session.flush()
        shared_target = SourceImage(batch=target, source_relative_path="用户原目录/共享.png", original_name="共享.png", safe_name="shared.png", relative_path="imports/shared.png", sha256="a" * 64, mime_type="image/png", width=50, height=50, file_size=shared.stat().st_size, order_index=0, ocr_status="completed")
        unique_target = SourceImage(batch=target, source_relative_path="用户原目录/独占.png", original_name="独占.png", safe_name="unique.png", relative_path="imports/unique.png", sha256="b" * 64, mime_type="image/png", width=50, height=50, file_size=unique.stat().st_size, order_index=1, ocr_status="completed")
        session.add_all([shared_target, unique_target])
        session.flush()
        shared_survivor = SourceImage(batch=survivor, source_relative_path="survivor.png", original_name="共享.png", safe_name="shared.png", relative_path="imports/shared.png", sha256="a" * 64, mime_type="image/png", width=50, height=50, file_size=shared.stat().st_size, duplicate_of_id=shared_target.id, order_index=0, ocr_status="duplicate")
        session.add(shared_survivor)
        session.flush()
        question = _candidate(target, unique_target, category, "待删题")
        survivor_question = _candidate(survivor, shared_survivor, category, "保留题")
        session.add_all([question, survivor_question])
        session.flush()
        part = CandidateImagePart(candidate_id=question.id, source_image_id=unique_target.id, crop_path="crops/delete-me.png", source_y_start=0, source_y_end=50, part_index=0)
        session.add_all([
            part,
            WrongBookEntry(question_id=question.id),
            ReviewAction(batch_id=target.id, candidate_id=question.id, target_type="candidate", target_id=question.id, action_type="update", before_json={}, after_json={}),
            OrderingIssue(batch_id=target.id, code="TEST", message="测试", image_ids_json=[unique_target.id]),
            ScreenshotOverlap(batch_id=target.id, source_image_id=shared_target.id, target_image_id=unique_target.id, text_score=0.5, visual_score=0.5, combined_score=0.5, overlap_ratio=0.2, matched_text_json=[]),
        ])
        session.commit()
        return database, settings, target.id, survivor.id, question.id, survivor_question.id, shared_survivor.id, user_original, shared, unique, crop


def test_delete_batch_removes_database_and_exclusive_files_but_keeps_shared_and_user_files(tmp_path: Path) -> None:
    database, settings, target_id, survivor_id, question_id, survivor_question_id, survivor_image_id, user_original, shared, unique, crop = _database_and_seed(tmp_path)
    with database.session_factory() as session:
        result = delete_import_batch(session, settings, target_id)

    with database.session_factory() as session:
        assert session.get(ImportBatch, target_id) is None
        assert session.get(QuestionCandidate, question_id) is None
        assert session.get(ImportBatch, survivor_id) is not None
        assert session.get(QuestionCandidate, survivor_question_id) is not None
        assert session.get(SourceImage, survivor_image_id).duplicate_of_id is None
        assert session.query(WrongBookEntry).count() == 0
        assert session.query(ReviewAction).count() == 0
        assert session.query(OrderingIssue).count() == 0
        assert session.query(ScreenshotOverlap).count() == 0
    assert result.deleted_questions == 1 and result.deleted_images == 2
    assert shared.is_file()
    assert user_original.is_file()
    assert not unique.exists() and not crop.exists()


def test_delete_batch_rejects_unsafe_internal_path_without_touching_database_or_user_file(tmp_path: Path) -> None:
    database, settings, target_id, _, _, _, _, user_original, _, _, _ = _database_and_seed(tmp_path)
    with database.session_factory() as session:
        image = session.query(SourceImage).filter(SourceImage.batch_id == target_id).first()
        image.relative_path = "../user-originals/不要删除.png"
        session.commit()
    with database.session_factory() as session:
        with pytest.raises(LibraryDeletionError, match="路径"):
            delete_import_batch(session, settings, target_id)
    with database.session_factory() as session:
        assert session.get(ImportBatch, target_id) is not None
    assert user_original.is_file()


def test_delete_batch_rejects_invalid_practice_snapshot_before_staging(
    tmp_path: Path,
) -> None:
    database, settings, target_id, _, question_id, _, _, _, _, unique, crop = (
        _database_and_seed(tmp_path)
    )
    with database.session_factory() as session:
        practice = PracticeSession(
            feedback_mode="deferred",
            order_mode="sequential",
            selection_json={"batch_id": target_id},
            requested_count=1,
            actual_count=1,
            limit_adjusted=False,
            status="active",
        )
        session.add(
            PracticeItem(
                session=practice,
                question_id=question_id,
                order_index=0,
                question_snapshot_json={"question_id": question_id},
                answer_snapshot_json="A",
                explanation_snapshot=None,
                image_parts_snapshot_json=[
                    {"part_index": 0, "crop_path": "crops/../outside.png"}
                ],
            )
        )
        session.commit()

    with database.session_factory() as session:
        with pytest.raises(
            LibraryDeletionError,
            match="练习图片快照无效，已取消删除",
        ):
            delete_import_batch(session, settings, target_id)

    with database.session_factory() as session:
        assert session.get(ImportBatch, target_id) is not None
        assert session.get(QuestionCandidate, question_id) is not None
    assert unique.is_file() and crop.is_file()
    assert not (settings.data_dir / ".delete-staging").exists()


@pytest.mark.parametrize(
    ("stored_value", "expected_cause"),
    [
        ("{", json.JSONDecodeError),
        (b"\xff", UnicodeDecodeError),
    ],
    ids=["invalid-json-syntax", "invalid-utf8-blob"],
)
def test_delete_batch_maps_orm_snapshot_decode_failures_before_staging(
    tmp_path: Path,
    stored_value: object,
    expected_cause: type[Exception],
) -> None:
    (
        database,
        settings,
        target_id,
        _,
        question_id,
        _,
        _,
        user_original,
        shared,
        unique,
        crop,
    ) = _database_and_seed(tmp_path)
    with database.session_factory() as session:
        practice = PracticeSession(
            feedback_mode="deferred",
            order_mode="sequential",
            selection_json={"batch_id": target_id},
            requested_count=1,
            actual_count=1,
            limit_adjusted=False,
            status="active",
        )
        item = PracticeItem(
            session=practice,
            question_id=question_id,
            order_index=0,
            question_snapshot_json={"question_id": question_id},
            answer_snapshot_json="A",
            explanation_snapshot=None,
            image_parts_snapshot_json=[],
        )
        session.add(item)
        session.commit()
        item_id = item.id

    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE practice_items SET image_parts_snapshot_json = ? WHERE id = ?",
            (stored_value, item_id),
        )

    with database.session_factory() as session:
        with pytest.raises(LibraryDeletionError) as exc_info:
            delete_import_batch(session, settings, target_id)

    assert str(exc_info.value) == "练习图片快照无效，已取消删除"
    assert type(exc_info.value.__cause__) is expected_cause
    with database.session_factory() as session:
        assert session.get(ImportBatch, target_id) is not None
        assert session.get(QuestionCandidate, question_id) is not None
    with database.engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT id FROM practice_items WHERE id = ?", (item_id,)
        ).scalar_one() == item_id
    assert user_original.is_file() and shared.is_file()
    assert unique.is_file() and crop.is_file()
    assert not (settings.data_dir / ".delete-staging").exists()


def test_delete_batch_maps_snapshot_parser_type_error_before_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, settings, target_id, _, question_id, _, _, _, _, unique, crop = (
        _database_and_seed(tmp_path)
    )
    monkeypatch.setattr(
        "app.services.library.batch_delete.collect_practice_snapshot_crop_paths",
        lambda snapshots: (_ for _ in ()).throw(TypeError("invalid decoded value")),
    )

    with database.session_factory() as session:
        with pytest.raises(LibraryDeletionError) as exc_info:
            delete_import_batch(session, settings, target_id)

    assert str(exc_info.value) == "练习图片快照无效，已取消删除"
    assert type(exc_info.value.__cause__) is TypeError
    with database.session_factory() as session:
        assert session.get(ImportBatch, target_id) is not None
        assert session.get(QuestionCandidate, question_id) is not None
    assert unique.is_file() and crop.is_file()
    assert not (settings.data_dir / ".delete-staging").exists()


def test_delete_batch_does_not_map_unrelated_snapshot_parser_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, settings, target_id, *_ = _database_and_seed(tmp_path)
    monkeypatch.setattr(
        "app.services.library.batch_delete.collect_practice_snapshot_crop_paths",
        lambda snapshots: (_ for _ in ()).throw(RuntimeError("unexpected failure")),
    )

    with database.session_factory() as session:
        with pytest.raises(RuntimeError, match="unexpected failure"):
            delete_import_batch(session, settings, target_id)


def test_delete_batch_commit_failure_leaves_practice_referenced_crop_in_place(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, settings, target_id, _, question_id, _, _, _, shared, unique, crop = (
        _database_and_seed(tmp_path)
    )
    with database.session_factory() as session:
        practice = PracticeSession(
            feedback_mode="deferred",
            order_mode="sequential",
            selection_json={"batch_id": target_id},
            requested_count=1,
            actual_count=1,
            limit_adjusted=False,
            status="active",
        )
        session.add(
            PracticeItem(
                session=practice,
                question_id=question_id,
                order_index=0,
                question_snapshot_json={"question_id": question_id},
                answer_snapshot_json="A",
                explanation_snapshot=None,
                image_parts_snapshot_json=[
                    {"part_index": 0, "crop_path": "crops/delete-me.png"}
                ],
            )
        )
        session.commit()

    with database.session_factory() as session:
        monkeypatch.setattr(
            session,
            "commit",
            lambda: (_ for _ in ()).throw(RuntimeError("commit failed")),
        )
        with pytest.raises(RuntimeError, match="commit failed"):
            delete_import_batch(session, settings, target_id)

    assert shared.is_file() and unique.is_file() and crop.is_file()
    assert not (settings.data_dir / ".delete-staging").exists()


def test_delete_batch_restores_staged_files_when_database_commit_fails(tmp_path: Path, monkeypatch) -> None:
    database, settings, target_id, _, _, _, _, _, shared, unique, crop = _database_and_seed(tmp_path)
    with database.session_factory() as session:
        monkeypatch.setattr(session, "commit", lambda: (_ for _ in ()).throw(RuntimeError("commit failed")))
        with pytest.raises(RuntimeError, match="commit failed"):
            delete_import_batch(session, settings, target_id)
    with database.session_factory() as session:
        assert session.get(ImportBatch, target_id) is not None
    assert shared.is_file() and unique.is_file() and crop.is_file()


def test_delete_batch_restores_files_when_commit_is_interrupted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (
        database,
        settings,
        target_id,
        _,
        _,
        _,
        _,
        user_original,
        shared,
        unique,
        crop,
    ) = _database_and_seed(tmp_path)
    with database.session_factory() as session:
        monkeypatch.setattr(
            session,
            "commit",
            lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        with pytest.raises(KeyboardInterrupt):
            delete_import_batch(session, settings, target_id)

    with database.session_factory() as session:
        assert session.get(ImportBatch, target_id) is not None
    assert user_original.is_file() and shared.is_file()
    assert unique.is_file() and crop.is_file()
    assert not (settings.data_dir / ".delete-staging").exists()


def test_delete_batch_reports_cleanup_pending_and_startup_retries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (
        database,
        settings,
        target_id,
        _,
        _,
        _,
        _,
        user_original,
        shared,
        unique,
        crop,
    ) = _database_and_seed(tmp_path)
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
    with database.session_factory() as session:
        result = delete_import_batch(session, settings, target_id)

    assert result.cleanup_pending is True
    with database.session_factory() as session:
        assert session.get(ImportBatch, target_id) is None
    operation_roots = list((settings.data_dir / ".delete-staging").iterdir())
    assert len(operation_roots) == 1
    assert (operation_roots[0] / "operation.json").is_file()
    assert user_original.is_file() and shared.is_file()
    assert not unique.exists() and not crop.exists()

    monkeypatch.undo()
    with database.session_factory() as session:
        report = BatchDeletionJournal(settings.data_dir).recover(session)

    assert report.cleaned_operations == 1
    assert not (settings.data_dir / ".delete-staging").exists()


def test_cleanup_keeps_manifest_until_staged_file_tree_is_removed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _, settings, target_id, *_, unique, _ = _database_and_seed(tmp_path)
    journal = BatchDeletionJournal(settings.data_dir)
    operation = journal.prepare(target_id, {"imports/unique.png"})
    journal.stage(operation)
    real_rmtree = __import__("shutil").rmtree

    def fail_files_cleanup(path, *args, **kwargs):
        candidate = Path(path)
        if candidate == operation.files_root:
            raise OSError("injected staged file cleanup failure")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(
        "app.services.library.deletion_recovery.shutil.rmtree",
        fail_files_cleanup,
    )

    assert journal.cleanup(operation) is False
    assert (operation.root / "operation.json").is_file()
    assert (operation.files_root / "imports" / "unique.png").is_file()
    assert not unique.exists()


def test_startup_recovery_removes_empty_manifestless_operation(
    tmp_path: Path,
) -> None:
    database, settings, *_ = _database_and_seed(tmp_path)
    operation_root = settings.data_dir / ".delete-staging" / str(uuid4())
    operation_root.mkdir(parents=True)
    journal = BatchDeletionJournal(settings.data_dir)

    with database.session_factory() as session:
        report = journal.recover(session)

    assert report.cleaned_operations == 1
    assert not operation_root.exists()
    assert not (settings.data_dir / ".delete-staging").exists()


def test_startup_recovery_restores_partially_staged_files_when_batch_exists(
    tmp_path: Path,
) -> None:
    (
        database,
        settings,
        target_id,
        _,
        _,
        _,
        _,
        user_original,
        shared,
        unique,
        crop,
    ) = _database_and_seed(tmp_path)
    journal = BatchDeletionJournal(settings.data_dir)
    operation = journal.prepare(
        target_id,
        {"imports/unique.png", "crops/delete-me.png"},
    )
    staged_unique = operation.files_root / "imports" / "unique.png"
    staged_unique.parent.mkdir(parents=True)
    os.replace(unique, staged_unique)

    with database.session_factory() as session:
        report = journal.recover(session)

    assert report.restored_operations == 1
    assert user_original.is_file() and shared.is_file()
    assert unique.is_file() and crop.is_file()
    assert not operation.root.exists()


def test_startup_recovery_cleans_staged_files_when_batch_is_absent(
    tmp_path: Path,
) -> None:
    (
        database,
        settings,
        _,
        _,
        _,
        _,
        _,
        user_original,
        shared,
        unique,
        crop,
    ) = _database_and_seed(tmp_path)
    journal = BatchDeletionJournal(settings.data_dir)
    operation = journal.prepare(str(uuid4()), {"imports/unique.png"})
    journal.stage(operation)

    with database.session_factory() as session:
        report = journal.recover(session)

    assert report.cleaned_operations == 1
    assert user_original.is_file() and shared.is_file() and crop.is_file()
    assert not unique.exists()
    assert not operation.root.exists()


def test_prepare_rejects_staging_root_resolved_outside_data_dir(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    outside = tmp_path / "outside"
    data_dir.mkdir()
    outside.mkdir()
    journal = BatchDeletionJournal(data_dir)
    journal.staging_root = outside

    with pytest.raises(BatchDeletionRecoveryError):
        journal.prepare(
            "11111111-1111-4111-8111-111111111111",
            set(),
        )

    assert list(outside.iterdir()) == []
    assert not (data_dir / ".delete-staging").exists()


def test_startup_recovery_retains_malformed_journal_and_fails_closed(
    tmp_path: Path,
) -> None:
    database, settings, *_ = _database_and_seed(tmp_path)
    operation_root = settings.data_dir / ".delete-staging" / "malformed"
    operation_root.mkdir(parents=True)
    (operation_root / "operation.json").write_text("{", encoding="utf-8")
    journal = BatchDeletionJournal(settings.data_dir)

    with database.session_factory() as session:
        with pytest.raises(BatchDeletionRecoveryError, match="清单"):
            journal.recover(session)

    assert operation_root.is_dir()


def test_startup_recovery_rejects_unsafe_journal_path_without_touching_files(
    tmp_path: Path,
) -> None:
    (
        database,
        settings,
        target_id,
        _,
        _,
        _,
        _,
        user_original,
        shared,
        unique,
        crop,
    ) = _database_and_seed(tmp_path)
    operation_id = str(uuid4())
    operation_root = settings.data_dir / ".delete-staging" / operation_id
    operation_root.mkdir(parents=True)
    (operation_root / "operation.json").write_text(
        json.dumps(
            {
                "version": 1,
                "operation_id": operation_id,
                "batch_id": target_id,
                "relative_paths": ["../user-originals/do-not-touch.png"],
            }
        ),
        encoding="utf-8",
    )
    journal = BatchDeletionJournal(settings.data_dir)

    with database.session_factory() as session:
        with pytest.raises(BatchDeletionRecoveryError, match="路径"):
            journal.recover(session)

    assert operation_root.is_dir()
    assert user_original.is_file() and shared.is_file()
    assert unique.is_file() and crop.is_file()


def test_startup_recovery_retains_live_and_staged_conflict(
    tmp_path: Path,
) -> None:
    (
        database,
        settings,
        target_id,
        _,
        _,
        _,
        _,
        user_original,
        shared,
        unique,
        crop,
    ) = _database_and_seed(tmp_path)
    journal = BatchDeletionJournal(settings.data_dir)
    operation = journal.prepare(target_id, {"imports/unique.png"})
    journal.stage(operation)
    unique.parent.mkdir(parents=True, exist_ok=True)
    unique.write_bytes(b"conflicting-live-file")

    with database.session_factory() as session:
        with pytest.raises(BatchDeletionRecoveryError, match="冲突"):
            journal.recover(session)

    assert operation.root.is_dir()
    assert (operation.files_root / "imports" / "unique.png").is_file()
    assert unique.read_bytes() == b"conflicting-live-file"
    assert user_original.is_file() and shared.is_file() and crop.is_file()


def test_lifespan_cleans_committed_batch_deletion_residue(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (
        database,
        settings,
        target_id,
        _,
        _,
        _,
        _,
        user_original,
        shared,
        unique,
        crop,
    ) = _database_and_seed(tmp_path)
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
    with database.session_factory() as session:
        result = delete_import_batch(session, settings, target_id)
    assert result.cleanup_pending is True
    assert (settings.data_dir / ".delete-staging").is_dir()
    monkeypatch.undo()
    database.dispose()

    app = create_app(settings, MockOcrEngine())
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200

    assert user_original.is_file() and shared.is_file()
    assert not unique.exists() and not crop.exists()
    assert not (settings.data_dir / ".delete-staging").exists()


def test_lifespan_fails_closed_and_retains_corrupt_deletion_journal(
    tmp_path: Path,
) -> None:
    database, settings, *_ = _database_and_seed(tmp_path)
    operation_root = settings.data_dir / ".delete-staging" / str(uuid4())
    operation_root.mkdir(parents=True)
    (operation_root / "operation.json").write_text("{", encoding="utf-8")
    database.dispose()
    app = create_app(settings, MockOcrEngine())

    with pytest.raises(BatchDeletionRecoveryError, match="清单"):
        with TestClient(app):
            pass

    assert operation_root.is_dir()


def test_batch_delete_api_requires_confirmation_and_deletes_batch(tmp_path: Path) -> None:
    database, settings, target_id, _, _, _, _, user_original, shared, unique, crop = _database_and_seed(tmp_path)
    database.dispose()
    app = create_app(settings, MockOcrEngine())
    with TestClient(app) as client:
        unconfirmed = client.request("DELETE", f"/api/batches/{target_id}", json={"confirmed": False})
        deleted = client.request("DELETE", f"/api/batches/{target_id}", json={"confirmed": True})
    assert unconfirmed.status_code == 422
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True
    assert user_original.is_file() and shared.is_file()
    assert not unique.exists() and not crop.exists()
