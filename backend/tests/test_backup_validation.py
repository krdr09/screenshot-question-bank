from __future__ import annotations

import json
import sqlite3
import stat
import struct
from hashlib import sha256
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest
from sqlalchemy import text

from app.config import Settings
from app.database import Database
from app.models import (
    CandidateImagePart,
    Category,
    Course,
    ImportBatch,
    PracticeItem,
    PracticeSession,
    QuestionCandidate,
    SourceImage,
)
from app.services.backups.archive import BackupArchiveService
from app.services.backups.errors import (
    BackupTokenExpiredError,
    BackupValidationError,
)
from app.services.backups.manifest import database_counts
from app.services.library.batch_delete import delete_import_batch
from app.services.backups.validation import ArchiveValidator


def validation_types():
    from app.services.backups.tokens import RestoreTokenRegistry
    from app.services.backups.validation import ArchiveValidator, ValidatedBackup

    return ArchiveValidator, ValidatedBackup, RestoreTokenRegistry


@pytest.fixture
def valid_archive(tmp_path: Path):
    data_dir = tmp_path / "data"
    database_path = data_dir / "app.db"
    settings = Settings(
        data_dir=data_dir,
        database_url=f"sqlite:///{database_path.as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing-dist",
        _env_file=None,
    )
    database = Database(settings)
    database.create_schema()
    with database.engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('20260714_0007')")
        )
    detail = BackupArchiveService(database, settings).create()
    database.dispose()
    return settings, data_dir / "backups" / detail.filename


@pytest.fixture
def referenced_archive(tmp_path: Path):
    data_dir = tmp_path / "referenced-data"
    database_path = data_dir / "app.db"
    settings = Settings(
        data_dir=data_dir,
        database_url=f"sqlite:///{database_path.as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing-dist",
        _env_file=None,
    )
    database = Database(settings)
    database.create_schema()
    with database.engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('20260714_0007')")
        )
    source_path = data_dir / "imports" / "source.png"
    crop_path = data_dir / "crops" / "question.png"
    source_path.parent.mkdir(parents=True)
    crop_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"referenced-source")
    crop_path.write_bytes(b"referenced-crop")
    with database.session_factory() as session:
        course = Course(name="引用完整性课程")
        category = Category(course=course, path="阶段九")
        batch = ImportBatch(
            course=course,
            category=category,
            name="引用完整性批次",
            source_relative_path="阶段九/引用完整性批次",
            status="ready",
        )
        session.add(batch)
        session.flush()
        source = SourceImage(
            batch=batch,
            source_relative_path="source.png",
            original_name="source.png",
            safe_name="source.png",
            relative_path="imports/source.png",
            sha256=sha256(source_path.read_bytes()).hexdigest(),
            mime_type="image/png",
            width=10,
            height=10,
            file_size=source_path.stat().st_size,
            order_index=0,
            ocr_status="completed",
            ocr_result_json={"blocks": [{"text": "引用完整性"}]},
        )
        session.add(source)
        session.flush()
        candidate = QuestionCandidate(
            batch_id=batch.id,
            category_id=category.id,
            source_image_id=source.id,
            source_display_number="1",
            question_type="single_choice",
            stem="引用图片是否完整？",
            options_json=[{"label": "A", "text": "是"}],
            correct_answer_json="A",
            raw_ocr_text="1. 引用图片是否完整？",
            normalized_ocr_text="1. 引用图片是否完整？",
            ocr_confidence=0.99,
            parse_confidence=0.99,
            display_mode="text_first",
            anomaly_flags_json=[],
            status="ready",
        )
        session.add(candidate)
        session.flush()
        session.add(
            CandidateImagePart(
                candidate_id=candidate.id,
                source_image_id=source.id,
                crop_path="crops/question.png",
                source_y_start=0,
                source_y_end=10,
                part_index=0,
            )
        )
        session.commit()
        source_id = source.id
    detail = BackupArchiveService(database, settings).create()
    database.dispose()
    return (
        settings,
        data_dir / "backups" / detail.filename,
        f"payload/ocr/{source_id}.json",
    )


@pytest.fixture
def practice_snapshot_archive(tmp_path: Path):
    data_dir = tmp_path / "practice-snapshot-data"
    database_path = data_dir / "app.db"
    settings = Settings(
        data_dir=data_dir,
        database_url=f"sqlite:///{database_path.as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing-dist",
        _env_file=None,
    )
    database = Database(settings)
    database.create_schema()
    with database.engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('20260714_0007')")
        )
    crop_path = data_dir / "crops" / "history.png"
    crop_path.parent.mkdir(parents=True)
    crop_path.write_bytes(b"practice-history-crop")
    with database.session_factory() as session:
        course = Course(name="练习快照课程")
        category = Category(course=course, path="练习历史")
        batch = ImportBatch(
            course=course,
            category=category,
            name="练习快照批次",
            source_relative_path="练习快照课程/练习历史/练习快照批次",
            status="ready",
        )
        session.add(batch)
        session.flush()
        source = SourceImage(
            batch=batch,
            source_relative_path="history.png",
            original_name="history.png",
            safe_name="history.png",
            relative_path="imports/history.png",
            sha256="a" * 64,
            mime_type="image/png",
            width=10,
            height=10,
            file_size=1,
            order_index=0,
            ocr_status="completed",
        )
        candidate = QuestionCandidate(
            batch_id=batch.id,
            category_id=category.id,
            source_image=source,
            source_display_number="1",
            question_type="single_choice",
            stem="练习快照题目",
            options_json=[{"label": "A", "text": "答案"}],
            correct_answer_json="A",
            raw_ocr_text="练习快照题目",
            normalized_ocr_text="练习快照题目",
            ocr_confidence=1,
            parse_confidence=1,
            display_mode="image_preferred",
            anomaly_flags_json=[],
            status="ready",
        )
        session.add_all([source, candidate])
        session.flush()
        session.add(
            CandidateImagePart(
                candidate_id=candidate.id,
                source_image_id=source.id,
                crop_path="crops/history.png",
                source_y_start=0,
                source_y_end=10,
                part_index=0,
            )
        )
        practice = PracticeSession(
            feedback_mode="immediate",
            order_mode="sequential",
            selection_json={"manual_question_ids": [candidate.id]},
            requested_count=1,
            actual_count=1,
            status="submitted",
        )
        session.add(
            PracticeItem(
                session=practice,
                question_id=candidate.id,
                order_index=0,
                question_snapshot_json={"stem": candidate.stem},
                answer_snapshot_json="A",
                image_parts_snapshot_json=[{"crop_path": "crops/history.png"}],
            )
        )
        session.commit()
        batch_id = batch.id

    with database.session_factory() as session:
        delete_import_batch(session, settings, batch_id)
        assert not session.query(CandidateImagePart).count()
        assert (data_dir / "crops/history.png").is_file()

    detail = BackupArchiveService(database, settings).create()
    database.dispose()
    return (
        settings,
        data_dir / "backups" / detail.filename,
        "payload/crops/history.png",
    )


def clone_settings(settings: Settings, **overrides) -> Settings:
    values = settings.model_dump()
    values.update(overrides)
    return Settings(**values, _env_file=None)


def rewrite_archive(
    source: Path,
    target: Path,
    *,
    replacements: dict[str, bytes] | None = None,
    manifest_updates: dict | None = None,
    extra: list[tuple[str | ZipInfo, bytes]] | None = None,
    remove: set[str] | None = None,
    refresh_hashes: bool = False,
) -> Path:
    replacements = replacements or {}
    remove = remove or set()
    with ZipFile(source) as bundle:
        entries = [
            (info, bundle.read(info.filename))
            for info in bundle.infolist()
            if info.filename not in remove
        ]
    manifest = json.loads(next(data for info, data in entries if info.filename == "manifest.json"))
    if manifest_updates:
        manifest.update(manifest_updates)
    payloads = {
        info.filename: replacements.get(info.filename, data)
        for info, data in entries
        if info.filename != "manifest.json"
    }
    if refresh_hashes:
        manifest["files"] = [
            item for item in manifest["files"] if item["path"] in payloads
        ]
        for item in manifest["files"]:
            data = payloads[item["path"]]
            item["size"] = len(data)
            item["sha256"] = sha256(data).hexdigest()
        manifest["file_count"] = len(manifest["files"])
        manifest["uncompressed_bytes"] = sum(item["size"] for item in manifest["files"])
    with ZipFile(target, "w", compression=ZIP_DEFLATED) as bundle:
        bundle.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        )
        for info, data in entries:
            if info.filename == "manifest.json" or info.filename in remove:
                continue
            bundle.writestr(info, replacements.get(info.filename, data))
        for name, data in extra or []:
            bundle.writestr(name, data)
    return target


def change_database(
    archive: Path,
    target: Path,
    tmp_path: Path,
    statements: list[str],
    *,
    revision: str | None = None,
) -> Path:
    database_file = tmp_path / f"changed-{target.stem}.db"
    with ZipFile(archive) as bundle:
        database_file.write_bytes(bundle.read("payload/app.db"))
    with sqlite3.connect(database_file) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        for statement in statements:
            connection.execute(statement)
        if revision:
            connection.execute("UPDATE alembic_version SET version_num = ?", (revision,))
        connection.commit()
    updates = {"schema_revision": revision} if revision else None
    return rewrite_archive(
        archive,
        target,
        replacements={"payload/app.db": database_file.read_bytes()},
        manifest_updates=updates,
        refresh_hashes=True,
    )


def validate(settings: Settings, archive: Path, staging: Path):
    ArchiveValidator, _, _ = validation_types()
    return ArchiveValidator(settings).validate(archive, staging)


def patch_encrypted_flags(path: Path) -> None:
    data = bytearray(path.read_bytes())
    start = 0
    while (offset := data.find(b"PK\x03\x04", start)) >= 0:
        flags = struct.unpack_from("<H", data, offset + 6)[0] | 1
        struct.pack_into("<H", data, offset + 6, flags)
        start = offset + 4
    start = 0
    while (offset := data.find(b"PK\x01\x02", start)) >= 0:
        flags = struct.unpack_from("<H", data, offset + 8)[0] | 1
        struct.pack_into("<H", data, offset + 8, flags)
        start = offset + 4
    path.write_bytes(data)


def test_database_counts_uses_exact_rows_and_zeroes_absent_legacy_tables(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-counts.db"
    present_rows = {
        "question_candidates": 2,
        "source_images": 1,
        "candidate_image_parts": 3,
        "wrong_book_entries": 4,
    }
    with sqlite3.connect(database_path) as connection:
        for table, count in present_rows.items():
            connection.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY)')
            connection.executemany(
                f'INSERT INTO "{table}" (id) VALUES (?)',
                [(index,) for index in range(1, count + 1)],
            )
        connection.commit()

    counts = database_counts(
        database_path,
        available_tables=set(present_rows),
    )

    assert counts.model_dump() == {
        "questions": 2,
        "source_images": 1,
        "crop_images": 3,
        "attempts": 0,
        "wrong_questions": 4,
    }


def test_valid_archive_extracts_and_reports_compatibility(valid_archive, tmp_path) -> None:
    settings, archive = valid_archive

    result = validate(settings, archive, tmp_path / "staging")

    assert result.manifest.schema_revision == "20260714_0007"
    assert result.migration_required is True
    assert result.warnings == ("恢复后启动时将升级数据库结构",)
    assert (result.staging_dir / "payload" / "app.db").is_file()


@pytest.mark.parametrize(
    "member",
    [
        "../escape.txt",
        "/absolute.txt",
        "C:/drive.txt",
        "//server/share.txt",
        "payload\\backslash.txt",
        "payload/./dot.txt",
        "payload//empty.txt",
    ],
)
def test_validator_rejects_unsafe_member_paths(
    valid_archive, tmp_path, member
) -> None:
    settings, archive = valid_archive
    malicious = rewrite_archive(
        archive,
        tmp_path / "unsafe.zip",
        extra=[(member, b"escape")],
    )

    with pytest.raises(BackupValidationError):
        validate(settings, malicious, tmp_path / "staging")

    assert not (tmp_path / "escape.txt").exists()


def test_validator_rejects_duplicate_symlink_and_encrypted_members(
    valid_archive, tmp_path
) -> None:
    settings, archive = valid_archive
    duplicate = tmp_path / "duplicate.zip"
    duplicate.write_bytes(archive.read_bytes())
    with pytest.warns(UserWarning, match="Duplicate name"):
        with ZipFile(duplicate, "a") as bundle:
            bundle.writestr("payload/app.db", b"duplicate")
    with pytest.raises(BackupValidationError):
        validate(settings, duplicate, tmp_path / "duplicate-staging")

    link = ZipInfo("payload/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    linked = rewrite_archive(
        archive,
        tmp_path / "link.zip",
        extra=[(link, b"../../outside")],
    )
    with pytest.raises(BackupValidationError):
        validate(settings, linked, tmp_path / "link-staging")

    encrypted = rewrite_archive(
        archive,
        tmp_path / "encrypted.zip",
        extra=[("payload/encrypted", b"secret")],
    )
    patch_encrypted_flags(encrypted)
    with pytest.raises(BackupValidationError):
        validate(settings, encrypted, tmp_path / "encrypted-staging")


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("max_backup_entries", 1),
        ("max_backup_member_bytes", 8),
        ("max_backup_uncompressed_bytes", 8),
        ("max_backup_upload_bytes", 8),
        ("max_backup_path_length", 8),
    ],
)
def test_validator_enforces_configured_archive_limits(
    valid_archive, tmp_path, setting, value
) -> None:
    settings, archive = valid_archive
    limited = clone_settings(settings, **{setting: value})

    with pytest.raises(BackupValidationError):
        validate(limited, archive, tmp_path / setting)


def test_validator_rejects_excessive_compression_ratio(valid_archive, tmp_path) -> None:
    settings, archive = valid_archive
    compressed = rewrite_archive(
        archive,
        tmp_path / "compressed.zip",
        extra=[("payload/compressed.bin", b"0" * 1024 * 1024)],
    )
    limited = clone_settings(settings, max_backup_compression_ratio=2.0)

    with pytest.raises(BackupValidationError):
        validate(limited, compressed, tmp_path / "staging")


@pytest.mark.parametrize("mutation", ["missing", "extra", "tampered", "format"])
def test_validator_rejects_manifest_membership_hash_and_format_mutations(
    valid_archive, tmp_path, mutation
) -> None:
    settings, archive = valid_archive
    target = tmp_path / f"{mutation}.zip"
    if mutation == "missing":
        mutated = rewrite_archive(
            archive,
            target,
            remove={"payload/config/migratable-settings.json"},
        )
    elif mutation == "extra":
        mutated = rewrite_archive(
            archive, target, extra=[("payload/extra.txt", b"extra")]
        )
    elif mutation == "tampered":
        mutated = rewrite_archive(
            archive,
            target,
            replacements={"payload/config/migratable-settings.json": b"{}"},
        )
    else:
        mutated = rewrite_archive(
            archive, target, manifest_updates={"backup_format_version": 2}
        )

    with pytest.raises(BackupValidationError):
        validate(settings, mutated, tmp_path / "staging")


def test_validator_rejects_corrupt_missing_table_and_foreign_key_database(
    valid_archive, tmp_path
) -> None:
    settings, archive = valid_archive
    corrupt = rewrite_archive(
        archive,
        tmp_path / "corrupt.zip",
        replacements={"payload/app.db": b"not sqlite"},
        refresh_hashes=True,
    )
    missing = change_database(
        archive,
        tmp_path / "missing-table.zip",
        tmp_path,
        ["DROP TABLE courses"],
    )
    foreign = change_database(
        archive,
        tmp_path / "foreign.zip",
        tmp_path,
        [
            "INSERT INTO categories (id, course_id, path, created_at) "
            "VALUES ('invalid', 'missing-course', '无效', CURRENT_TIMESTAMP)"
        ],
    )

    for index, candidate in enumerate((corrupt, missing, foreign)):
        with pytest.raises(BackupValidationError):
            validate(settings, candidate, tmp_path / f"staging-{index}")


@pytest.mark.parametrize(
    "payload",
    [
        b"{",
        b"[]",
        b'{"ocr_device":"quantum"}',
        b'{"ocr_device":"cpu","unexpected":true}',
    ],
)
def test_validator_rejects_invalid_self_consistent_portable_settings(
    valid_archive, tmp_path, payload: bytes
) -> None:
    settings, archive = valid_archive
    invalid = rewrite_archive(
        archive,
        tmp_path / "invalid-config.zip",
        replacements={"payload/config/migratable-settings.json": payload},
        refresh_hashes=True,
    )

    with pytest.raises(BackupValidationError):
        validate(settings, invalid, tmp_path / "invalid-config-staging")


def test_validator_accepts_legacy_portable_settings_without_hash_review_margin(
    valid_archive, tmp_path
) -> None:
    settings, archive = valid_archive
    with ZipFile(archive) as bundle:
        payload = json.loads(bundle.read("payload/config/migratable-settings.json"))
    payload.pop("duplicate_hash_review_margin")
    legacy = rewrite_archive(
        archive,
        tmp_path / "legacy-config.zip",
        replacements={
            "payload/config/migratable-settings.json": json.dumps(payload).encode(
                "utf-8"
            )
        },
        refresh_hashes=True,
    )

    result = validate(settings, legacy, tmp_path / "legacy-config-staging")

    assert result.manifest.backup_format_version == 1


def test_validator_requires_portable_settings_even_with_consistent_manifest(
    valid_archive, tmp_path
) -> None:
    settings, archive = valid_archive
    missing = rewrite_archive(
        archive,
        tmp_path / "missing-config.zip",
        remove={"payload/config/migratable-settings.json"},
        refresh_hashes=True,
    )

    with pytest.raises(BackupValidationError):
        validate(settings, missing, tmp_path / "missing-config-staging")


def test_validator_rejects_manifest_counts_that_disagree_with_database(
    valid_archive,
    tmp_path: Path,
) -> None:
    settings, archive = valid_archive
    forged = rewrite_archive(
        archive,
        tmp_path / "forged-counts.zip",
        manifest_updates={
            "counts": {
                "questions": 999,
                "source_images": 999,
                "crop_images": 999,
                "attempts": 999,
                "wrong_questions": 999,
            }
        },
    )
    staging = tmp_path / "forged-counts-staging"

    with pytest.raises(
        BackupValidationError,
        match="^备份统计数量与数据库记录不一致$",
    ):
        validate(settings, forged, staging)

    assert not staging.exists()


@pytest.mark.parametrize("member", ["payload/imports/source.png", "payload/crops/question.png"])
def test_validator_rejects_missing_database_referenced_images(
    referenced_archive, tmp_path, member: str
) -> None:
    settings, archive, _ = referenced_archive
    missing = rewrite_archive(
        archive,
        tmp_path / f"missing-{Path(member).name}.zip",
        remove={member},
        refresh_hashes=True,
    )

    with pytest.raises(BackupValidationError):
        validate(settings, missing, tmp_path / f"staging-{Path(member).parent.name}")


def test_validator_rejects_missing_database_referenced_ocr_export(
    referenced_archive, tmp_path
) -> None:
    settings, archive, ocr_member = referenced_archive
    missing = rewrite_archive(
        archive,
        tmp_path / "missing-ocr.zip",
        remove={ocr_member},
        refresh_hashes=True,
    )

    with pytest.raises(BackupValidationError):
        validate(settings, missing, tmp_path / "missing-ocr-staging")


def test_validator_accepts_crop_referenced_only_by_practice_snapshot(
    practice_snapshot_archive, tmp_path: Path
) -> None:
    settings, archive, crop_member = practice_snapshot_archive

    validated = validate(settings, archive, tmp_path / "practice-snapshot-staging")

    assert crop_member in {entry.path for entry in validated.manifest.files}


def test_validator_rejects_missing_practice_snapshot_crop(
    practice_snapshot_archive, tmp_path: Path
) -> None:
    settings, archive, crop_member = practice_snapshot_archive
    missing = rewrite_archive(
        archive,
        tmp_path / "missing-practice-snapshot-crop.zip",
        remove={crop_member},
        refresh_hashes=True,
    )

    with pytest.raises(
        BackupValidationError,
        match="备份缺少数据库引用的练习裁切图",
    ):
        validate(settings, missing, tmp_path / "missing-practice-snapshot-staging")


@pytest.mark.parametrize(
    ("snapshot_value", "case"),
    [
        ("'{'", "invalid_json_syntax"),
        ("X'FF'", "invalid_utf8_bytes"),
        ("'{}'", "non_list_json"),
        (
            "'[{\"crop_path\": \"crops/../outside.png\"}]'",
            "unsafe_crop_path",
        ),
    ],
)
def test_validator_rejects_malformed_practice_snapshot(
    practice_snapshot_archive,
    tmp_path: Path,
    snapshot_value: str,
    case: str,
) -> None:
    settings, archive, _ = practice_snapshot_archive
    malformed = change_database(
        archive,
        tmp_path / f"malformed-practice-snapshot-{case}.zip",
        tmp_path,
        [
            "UPDATE practice_items SET image_parts_snapshot_json = "
            f"{snapshot_value}"
        ],
    )

    with pytest.raises(
        BackupValidationError,
        match="备份数据库包含无效的练习图片快照",
    ):
        validate(
            settings,
            malformed,
            tmp_path / f"malformed-practice-snapshot-{case}-staging",
        )


def test_validator_rejects_source_image_that_disagrees_with_database_hash(
    referenced_archive, tmp_path
) -> None:
    settings, archive, _ = referenced_archive
    changed = rewrite_archive(
        archive,
        tmp_path / "changed-source.zip",
        replacements={"payload/imports/source.png": b"different-source"},
        refresh_hashes=True,
    )

    with pytest.raises(BackupValidationError):
        validate(settings, changed, tmp_path / "changed-source-staging")


def test_staged_payload_validation_rejects_self_consistent_source_hash_mismatch(
    referenced_archive,
    tmp_path: Path,
) -> None:
    settings, archive, _ = referenced_archive
    extracted = ArchiveValidator(settings).validate(
        archive,
        tmp_path / "initial-validation",
    )
    payload_root = extracted.staging_dir / "payload"
    source_path = payload_root / "imports" / "source.png"
    changed = b"changed-after-database-snapshot"
    source_path.write_bytes(changed)

    files = [
        entry.model_copy(
            update={
                "size": len(changed),
                "sha256": sha256(changed).hexdigest(),
            }
        )
        if entry.path == "payload/imports/source.png"
        else entry
        for entry in extracted.manifest.files
    ]
    self_consistent_manifest = extracted.manifest.model_copy(
        update={
            "files": files,
            "uncompressed_bytes": sum(entry.size for entry in files),
        }
    )

    with pytest.raises(
        BackupValidationError,
        match="来源截图与数据库记录不一致",
    ):
        ArchiveValidator(settings).validate_staged_payload(
            database_path=payload_root / "app.db",
            manifest=self_consistent_manifest,
            payload_root=payload_root,
        )


def test_staged_payload_validation_rejects_manifest_count_mismatch(
    valid_archive,
    tmp_path: Path,
) -> None:
    settings, archive = valid_archive
    validator = ArchiveValidator(settings)
    validated = validator.validate(archive, tmp_path / "count-boundary-staging")
    forged_manifest = validated.manifest.model_copy(
        update={
            "counts": validated.manifest.counts.model_copy(
                update={"questions": validated.manifest.counts.questions + 1}
            )
        }
    )
    payload_root = validated.staging_dir / "payload"

    with pytest.raises(
        BackupValidationError,
        match="^备份统计数量与数据库记录不一致$",
    ):
        validator.validate_staged_payload(
            database_path=payload_root / "app.db",
            manifest=forged_manifest,
            payload_root=payload_root,
        )


def test_installed_database_validation_rejects_manifest_count_mismatch(
    valid_archive,
    tmp_path: Path,
) -> None:
    settings, archive = valid_archive
    validator = ArchiveValidator(settings)
    validated = validator.validate(archive, tmp_path / "installed-count-staging")
    forged = validated.__class__(
        manifest=validated.manifest.model_copy(
            update={
                "counts": validated.manifest.counts.model_copy(
                    update={"attempts": validated.manifest.counts.attempts + 1}
                )
            }
        ),
        staging_dir=validated.staging_dir,
        filename=validated.filename,
        migration_required=validated.migration_required,
        warnings=validated.warnings,
    )

    with pytest.raises(
        BackupValidationError,
        match="^备份统计数量与数据库记录不一致$",
    ):
        validator.validate_installed(
            validated.staging_dir / "payload" / "app.db",
            forged,
        )


def test_validator_validate_calls_shared_staged_payload_once(
    valid_archive,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, archive = valid_archive
    validator = ArchiveValidator(settings)
    calls = []
    original = validator.validate_staged_payload

    def record(*args, **kwargs) -> bool:
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(validator, "validate_staged_payload", record)

    validated = validator.validate(archive, tmp_path / "validate-staging")

    assert calls == [
        (
            (),
            {
                "database_path": validated.staging_dir / "payload" / "app.db",
                "manifest": validated.manifest,
                "payload_root": validated.staging_dir / "payload",
            },
        )
    ]


def test_validator_revalidate_calls_shared_staged_payload_once(
    valid_archive,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, archive = valid_archive
    validator = ArchiveValidator(settings)
    validated = validator.validate(archive, tmp_path / "initial-validation")
    calls = []
    original = validator.validate_staged_payload

    def record(*args, **kwargs) -> bool:
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(validator, "validate_staged_payload", record)

    validator.revalidate(validated)

    assert calls == [
        (
            (),
            {
                "database_path": validated.staging_dir / "payload" / "app.db",
                "manifest": validated.manifest,
                "payload_root": validated.staging_dir / "payload",
            },
        )
    ]


def test_validator_accepts_migratable_ancestor_and_rejects_unknown_revision(
    valid_archive, tmp_path
) -> None:
    settings, archive = valid_archive
    ancestor = change_database(
        archive,
        tmp_path / "ancestor.zip",
        tmp_path,
        [
            "DROP TABLE attempts",
            "DROP TABLE practice_items",
            "DROP TABLE practice_sessions",
            "ALTER TABLE wrong_book_entries DROP COLUMN updated_at",
            "ALTER TABLE wrong_book_entries DROP COLUMN mastered_at",
        ],
        revision="20260713_0006",
    )
    unknown = change_database(
        archive,
        tmp_path / "future.zip",
        tmp_path,
        [],
        revision="20990101_future",
    )

    accepted = validate(settings, ancestor, tmp_path / "ancestor-staging")
    assert accepted.migration_required is True
    assert accepted.warnings == ("恢复后启动时将升级数据库结构",)
    with sqlite3.connect(accepted.staging_dir / "payload" / "app.db") as database:
        tables = {
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "practice_sessions" not in tables

    wrong_counts = accepted.manifest.counts.model_dump()
    wrong_counts["attempts"] = 1
    wrong_ancestor = rewrite_archive(
        ancestor,
        tmp_path / "ancestor-wrong-counts.zip",
        manifest_updates={"counts": wrong_counts},
    )

    with pytest.raises(
        BackupValidationError,
        match="^备份统计数量与数据库记录不一致$",
    ):
        validate(
            settings,
            wrong_ancestor,
            tmp_path / "ancestor-wrong-counts-staging",
        )

    with pytest.raises(BackupValidationError):
        validate(settings, unknown, tmp_path / "future-staging")


def test_restore_tokens_are_single_use_expire_and_clean_staging(
    valid_archive, tmp_path
) -> None:
    settings, archive = valid_archive
    _, ValidatedBackup, RestoreTokenRegistry = validation_types()
    current_time = [100.0]
    validated = validate(settings, archive, tmp_path / "first-staging")
    registry = RestoreTokenRegistry(
        ttl_seconds=10,
        clock=lambda: current_time[0],
    )

    token = registry.issue(validated)
    assert registry.consume(token) == validated
    with pytest.raises(BackupTokenExpiredError):
        registry.consume(token)

    second = ValidatedBackup(
        manifest=validated.manifest,
        staging_dir=tmp_path / "expiring-staging",
        filename=validated.filename,
        migration_required=False,
        warnings=(),
    )
    second.staging_dir.mkdir()
    expiring = registry.issue(second)
    current_time[0] = 111.0
    registry.cleanup_expired()
    assert not second.staging_dir.exists()
    with pytest.raises(BackupTokenExpiredError):
        registry.consume(expiring)
