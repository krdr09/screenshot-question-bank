from __future__ import annotations

import importlib
import json
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from app.config import Settings
from app.database import Database
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
from app.services.backups.errors import BackupInternalError


def backup_schemas():
    return importlib.import_module("app.schemas.backups")


def archive_service_type():
    return importlib.import_module(
        "app.services.backups.archive"
    ).BackupArchiveService


@pytest.fixture
def backup_env(tmp_path: Path):
    data_dir = tmp_path / "data"
    database_path = data_dir / "app.db"
    current = Settings(
        data_dir=data_dir,
        database_url=f"sqlite:///{database_path.as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing-dist",
        OCR_ENGINE="mock",
        OCR_DEVICE="cpu",
        _env_file=None,
    )
    database = Database(current)
    database.create_schema()
    with database.engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('20260714_0007')")
        )

    import_path = data_dir / "imports" / "source.png"
    crop_path = data_dir / "crops" / "question.png"
    import_path.parent.mkdir(parents=True)
    crop_path.parent.mkdir(parents=True)
    import_path.write_bytes(b"synthetic-source-image")
    crop_path.write_bytes(b"synthetic-crop-image")
    (data_dir / "logs").mkdir()
    (data_dir / "logs" / "app.log").write_text("private path", encoding="utf-8")
    (data_dir / "backups").mkdir()
    (data_dir / "backups" / "unrelated.zip").write_bytes(b"not a backup")

    with database.session_factory() as session:
        course = Course(name="阶段九课程")
        category = Category(course=course, path="备份/恢复")
        batch_id = str(uuid4())
        batch = ImportBatch(
            id=batch_id,
            course=course,
            category=category,
            name="合成批次",
            source_relative_path="阶段九课程/备份/恢复",
            status="ready",
        )
        source = SourceImage(
            batch=batch,
            source_relative_path="source.png",
            original_name="source.png",
            safe_name="source.png",
            relative_path="imports/source.png",
            sha256=sha256(import_path.read_bytes()).hexdigest(),
            mime_type="image/png",
            width=100,
            height=200,
            file_size=import_path.stat().st_size,
            order_index=0,
            ocr_status="completed",
            ocr_result_json={"raw_output": {"mock": True}, "lines": []},
        )
        question = QuestionCandidate(
            batch_id=batch_id,
            category_id=category.id,
            source_image=source,
            source_display_number="1",
            question_type="single_choice",
            stem="备份中的题目",
            options_json=[{"label": "A", "text": "答案", "order_index": 0}],
            correct_answer_json="A",
            source_correct_answer_raw="A",
            explanation="合成解析",
            raw_ocr_text="1. 备份中的题目",
            normalized_ocr_text="1.备份中的题目",
            ocr_confidence=0.99,
            parse_confidence=0.99,
            display_mode="text_first",
            anomaly_flags_json=[],
            status="ready",
        )
        session.add_all([course, category, batch, source, question])
        session.flush()
        session.add(
            CandidateImagePart(
                candidate_id=question.id,
                source_image_id=source.id,
                crop_path="crops/question.png",
                source_y_start=0,
                source_y_end=100,
                part_index=0,
            )
        )
        practice = PracticeSession(
            feedback_mode="immediate",
            order_mode="sequential",
            selection_json={"manual_question_ids": [question.id]},
            requested_count=1,
            actual_count=1,
            status="submitted",
        )
        item = PracticeItem(
            session=practice,
            question_id=question.id,
            order_index=0,
            question_snapshot_json={"stem": question.stem},
            answer_snapshot_json="A",
            image_parts_snapshot_json=[],
        )
        attempt = Attempt(
            session=practice,
            item=item,
            selected_answer_json="B",
            is_correct=False,
        )
        session.add_all(
            [practice, item, attempt, WrongBookEntry(question_id=question.id)]
        )
        session.commit()
        source_id = source.id

    yield current, database, source_id
    database.dispose()


def test_backup_schema_module_exposes_all_api_contracts() -> None:
    schemas = backup_schemas()

    assert {
        "BackupCounts",
        "BackupDetail",
        "BackupFileEntry",
        "BackupListResponse",
        "BackupManifest",
        "BackupRestoreRequest",
        "BackupRestoreResult",
        "BackupValidationResult",
    } <= set(vars(schemas))


def test_restore_request_requires_explicit_confirmation() -> None:
    request_model = backup_schemas().BackupRestoreRequest

    with pytest.raises(ValidationError):
        request_model(validation_token="x" * 32, confirmed=False)


def test_manifest_requires_exact_sha256_and_positive_payload() -> None:
    schemas = backup_schemas()
    created_at = datetime.now(timezone.utc)
    entry = schemas.BackupFileEntry(
        path="payload/app.db",
        kind="database",
        size=4,
        sha256="a" * 64,
    )

    manifest = schemas.BackupManifest(
        backup_id=uuid4(),
        app_version="0.1.0",
        schema_revision="20260714_0007",
        created_at=created_at,
        file_count=1,
        uncompressed_bytes=4,
        counts=schemas.BackupCounts(),
        files=[entry],
    )

    assert manifest.backup_format_version == 1
    assert manifest.files == [entry]
    with pytest.raises(ValidationError):
        schemas.BackupFileEntry(
            path="payload/app.db",
            kind="database",
            size=4,
            sha256="not-a-hash",
        )


def test_archive_contains_snapshot_images_ocr_config_and_verified_manifest(
    backup_env,
) -> None:
    settings, database, source_id = backup_env
    service = archive_service_type()(database, settings)

    detail = service.create()
    archive = settings.data_dir / "backups" / detail.filename

    assert re.fullmatch(
        r"question-practice-backup-\d{8}-\d{6}(?:-\d+)?\.zip",
        detail.filename,
    )
    with ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        manifest = backup_schemas().BackupManifest.model_validate_json(
            bundle.read("manifest.json")
        )
        assert "payload/app.db" in names
        assert "payload/imports/source.png" in names
        assert "payload/crops/question.png" in names
        assert f"payload/ocr/{source_id}.json" in names
        assert "payload/config/migratable-settings.json" in names
        assert names == {"manifest.json", *(entry.path for entry in manifest.files)}
        for entry in manifest.files:
            payload = bundle.read(entry.path)
            assert len(payload) == entry.size
            assert sha256(payload).hexdigest() == entry.sha256
        assert not any("logs" in name for name in names)
        assert not any("backups" in name for name in names)
        ocr_payload = json.loads(bundle.read(f"payload/ocr/{source_id}.json"))
        portable = json.loads(
            bundle.read("payload/config/migratable-settings.json")
        )

    assert manifest.schema_revision == "20260714_0007"
    assert manifest.counts.model_dump() == {
        "questions": 1,
        "source_images": 1,
        "crop_images": 1,
        "attempts": 1,
        "wrong_questions": 1,
    }
    assert ocr_payload == {"lines": [], "raw_output": {"mock": True}}
    assert portable["ocr_engine"] == "mock"
    assert portable["ocr_device"] == "cpu"
    assert portable["duplicate_hash_review_margin"] == 2
    assert "data_dir" not in portable
    assert "database_url" not in portable
    assert "host" not in portable


def test_archive_creation_keeps_managed_copy_targets_below_windows_legacy_limit(
    backup_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, database, _ = backup_env
    database.dispose()
    target_data_dir_length = 130
    padding_length = target_data_dir_length - len(str(tmp_path.resolve())) - 1
    assert padding_length > 0
    long_data_dir = tmp_path / ("d" * padding_length)
    assert len(str(long_data_dir.resolve())) == target_data_dir_length
    shutil.copytree(settings.data_dir, long_data_dir)

    copied_settings = Settings(
        data_dir=long_data_dir,
        database_url=f"sqlite:///{(long_data_dir / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing-dist",
        OCR_ENGINE="mock",
        OCR_DEVICE="cpu",
        _env_file=None,
    )
    copied_database = Database(copied_settings)
    batch_directory = "a" * 36
    managed_filename = f"{'b' * 36}.png"
    nested_import = long_data_dir / "imports" / batch_directory / managed_filename
    nested_import.parent.mkdir(parents=True)
    (long_data_dir / "imports" / "source.png").replace(nested_import)
    with copied_database.engine.begin() as connection:
        connection.execute(
            text("UPDATE source_images SET relative_path = :relative_path"),
            {
                "relative_path": (
                    f"imports/{batch_directory}/{managed_filename}"
                )
            },
        )
    service = archive_service_type()(copied_database, copied_settings)
    archive_module = importlib.import_module("app.services.backups.archive")
    real_copy2 = archive_module.shutil.copy2
    managed_destinations: list[Path] = []

    def copy2_with_windows_legacy_limit(source, destination, *args, **kwargs):
        target = Path(destination)
        managed_destinations.append(target)
        if len(str(target.resolve())) >= 260:
            raise FileNotFoundError(
                3,
                "The system cannot find the path specified",
                str(target),
            )
        return real_copy2(source, destination, *args, **kwargs)

    monkeypatch.setattr(
        archive_module.shutil,
        "copy2",
        copy2_with_windows_legacy_limit,
    )
    try:
        detail = service.create()
    finally:
        copied_database.dispose()

    assert (copied_settings.data_dir / "backups" / detail.filename).is_file()
    assert managed_destinations
    assert max(len(str(path.resolve())) for path in managed_destinations) < 260
    assert not service.staging_dir.exists()


def test_archive_creation_retries_short_staging_token_collision(
    backup_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, database, _ = backup_env
    service = archive_service_type()(database, settings)
    archive_module = importlib.import_module("app.services.backups.archive")
    public_id = UUID("11111111-1111-1111-1111-111111111111")
    colliding_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    replacement_id = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    generated = iter([public_id, colliding_id, replacement_id])
    monkeypatch.setattr(archive_module, "uuid4", lambda: next(generated))
    collision_root = settings.data_dir / ".b" / colliding_id.hex[:12]
    collision_root.mkdir(parents=True)
    marker = collision_root / "owned-by-another-operation"
    marker.write_text("preserve", encoding="utf-8")

    detail = service.create()

    assert detail.backup_id == public_id
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert not (settings.data_dir / ".b" / replacement_id.hex[:12]).exists()


def test_archive_uses_committed_consistent_sqlite_snapshot(backup_env, tmp_path) -> None:
    settings, database, _ = backup_env
    service = archive_service_type()(database, settings)
    connection = database.engine.connect()
    transaction = connection.begin()
    connection.execute(
        text(
            "INSERT INTO courses (id, name, created_at, updated_at) "
            "VALUES ('uncommitted', '未提交课程', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    )

    detail = service.create()
    transaction.rollback()
    connection.close()

    extracted = tmp_path / "snapshot.db"
    with ZipFile(settings.data_dir / "backups" / detail.filename) as bundle:
        extracted.write_bytes(bundle.read("payload/app.db"))
    with sqlite3.connect(extracted) as snapshot:
        assert snapshot.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        names = {row[0] for row in snapshot.execute("SELECT name FROM courses")}
    assert names == {"阶段九课程"}


def test_archive_list_and_resolve_ignore_unmanaged_zip(backup_env) -> None:
    settings, database, _ = backup_env
    service = archive_service_type()(database, settings)
    created = service.create()

    listed = service.list()
    resolved = service.resolve(str(created.backup_id))

    assert [item.backup_id for item in listed] == [created.backup_id]
    assert resolved == settings.data_dir / "backups" / created.filename


def test_archive_rejects_source_change_after_snapshot_without_publishing(
    backup_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, database, _ = backup_env
    service = archive_service_type()(database, settings)
    existing = service.create()
    backups_dir = settings.data_dir / "backups"
    before = {
        path.name: path.read_bytes()
        for path in backups_dir.glob("question-practice-backup-*.zip")
    }
    assert set(before) == {existing.filename}

    source_path = settings.data_dir / "imports" / "source.png"
    original_snapshot = service._online_snapshot

    def snapshot_then_mutate(destination: Path) -> None:
        original_snapshot(destination)
        source_path.write_bytes(b"changed-after-database-snapshot")

    service._online_snapshot = snapshot_then_mutate
    write_calls = []
    publish_calls = []

    def reject_write(*args, **kwargs) -> None:
        write_calls.append((args, kwargs))
        pytest.fail("semantic rejection must happen before ZIP writing")

    def reject_publish(*args, **kwargs) -> None:
        publish_calls.append((args, kwargs))
        pytest.fail("semantic rejection must happen before archive publication")

    monkeypatch.setattr(service, "_write_archive", reject_write)
    archive_module = importlib.import_module("app.services.backups.archive")
    monkeypatch.setattr(archive_module.os, "replace", reject_publish)

    with pytest.raises(
        BackupInternalError,
        match="备份创建期间检测到数据不一致，未生成备份；"
        "请关闭可能修改数据目录的程序后重试",
    ):
        service.create()

    assert write_calls == []
    assert publish_calls == []

    after = {
        path.name: path.read_bytes()
        for path in backups_dir.glob("question-practice-backup-*.zip")
    }
    assert after == before
    assert not list(backups_dir.glob(".*.tmp"))
    assert not service.staging_dir.exists()


def test_archive_rejects_missing_practice_history_crop_without_publishing(
    backup_env,
) -> None:
    settings, database, _ = backup_env
    with database.session_factory() as session:
        practice = PracticeSession(
            feedback_mode="immediate",
            order_mode="sequential",
            selection_json={},
            requested_count=1,
            actual_count=1,
            status="submitted",
        )
        session.add(
            PracticeItem(
                session=practice,
                question_id=None,
                order_index=0,
                question_snapshot_json={"stem": "历史练习"},
                answer_snapshot_json="A",
                image_parts_snapshot_json=[
                    {"crop_path": "crops/missing-history.png"}
                ],
            )
        )
        session.commit()

    service = archive_service_type()(database, settings)
    with pytest.raises(
        BackupInternalError,
        match="备份创建期间检测到数据不一致，未生成备份；"
        "请关闭可能修改数据目录的程序后重试",
    ):
        service.create()

    backups_dir = settings.data_dir / "backups"
    assert not list(backups_dir.glob("question-practice-backup-*.zip"))
    assert not list(backups_dir.glob(".*.tmp"))
    assert not service.staging_dir.exists()
