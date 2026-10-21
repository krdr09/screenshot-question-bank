from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path, PurePosixPath

from app.schemas.backups import BackupManifest
from app.services.backups.errors import BackupValidationError
from app.services.practice.snapshot_files import (
    PracticeSnapshotReferenceError,
    collect_practice_snapshot_crop_paths,
)


def validate_database_payload(
    database_path: Path,
    manifest: BackupManifest,
    payload_root: Path,
    tables: set[str],
) -> None:
    entries = {entry.path: entry for entry in manifest.files}
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            source_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(source_images)")
            }
            ocr_column = (
                "ocr_result_json" if "ocr_result_json" in source_columns else "NULL"
            )
            sources = connection.execute(
                "SELECT id, relative_path, sha256, file_size, "
                f"{ocr_column} FROM source_images"
            )
            for source_id, relative_path, digest, size, raw_ocr in sources:
                member, file_path = database_referenced_file(
                    payload_root,
                    relative_path,
                    "imports",
                )
                entry = entries.get(member)
                if entry is None or entry.kind != "import" or not file_path.is_file():
                    raise BackupValidationError("备份缺少数据库引用的来源截图")
                if entry.sha256 != digest or entry.size != size:
                    raise BackupValidationError("来源截图与数据库记录不一致")
                if raw_ocr is not None:
                    _validate_ocr_export(
                        payload_root,
                        entries,
                        source_id,
                        raw_ocr,
                    )
            if "candidate_image_parts" in tables:
                for (relative_path,) in connection.execute(
                    "SELECT crop_path FROM candidate_image_parts"
                ):
                    member, file_path = database_referenced_file(
                        payload_root,
                        relative_path,
                        "crops",
                    )
                    entry = entries.get(member)
                    if entry is None or entry.kind != "crop" or not file_path.is_file():
                        raise BackupValidationError("备份缺少数据库引用的题目裁切图")
            if "practice_items" in tables:
                try:
                    snapshots = [
                        json.loads(snapshot)
                        if isinstance(snapshot, (str, bytes))
                        else snapshot
                        for (snapshot,) in connection.execute(
                            "SELECT image_parts_snapshot_json FROM practice_items"
                        )
                    ]
                    practice_crop_paths = collect_practice_snapshot_crop_paths(
                        snapshots
                    )
                except (
                    json.JSONDecodeError,
                    PracticeSnapshotReferenceError,
                    TypeError,
                    UnicodeDecodeError,
                ) as error:
                    raise BackupValidationError(
                        "备份数据库包含无效的练习图片快照"
                    ) from error
                for relative_path in practice_crop_paths:
                    member, file_path = database_referenced_file(
                        payload_root,
                        relative_path,
                        "crops",
                    )
                    entry = entries.get(member)
                    if entry is None or entry.kind != "crop" or not file_path.is_file():
                        raise BackupValidationError("备份缺少数据库引用的练习裁切图")
    except BackupValidationError:
        raise
    except (OSError, sqlite3.DatabaseError, json.JSONDecodeError, TypeError) as error:
        raise BackupValidationError("备份数据库的文件引用无效") from error


def _validate_ocr_export(
    payload_root: Path,
    entries: dict,
    source_id: object,
    raw_ocr: object,
) -> None:
    member = f"payload/ocr/{source_id}.json"
    entry = entries.get(member)
    path = payload_root / "ocr" / f"{source_id}.json"
    if entry is None or entry.kind != "ocr" or not path.is_file():
        raise BackupValidationError("备份缺少数据库引用的 OCR 原始结果")
    expected = json.loads(raw_ocr) if isinstance(raw_ocr, str) else raw_ocr
    if json.loads(path.read_bytes()) != expected:
        raise BackupValidationError("OCR 原始结果与数据库记录不一致")


def database_referenced_file(
    payload_root: Path,
    relative_path: object,
    expected_directory: str,
) -> tuple[str, Path]:
    if not isinstance(relative_path, str) or "\\" in relative_path:
        raise BackupValidationError("备份数据库包含不安全的文件路径")
    pure = PurePosixPath(relative_path)
    if (
        pure.is_absolute()
        or not pure.parts
        or pure.parts[0] != expected_directory
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise BackupValidationError("备份数据库包含不安全的文件路径")
    member = f"payload/{pure.as_posix()}"
    root = payload_root.resolve()
    file_path = (payload_root / Path(*pure.parts)).resolve()
    if root not in file_path.parents:
        raise BackupValidationError("备份数据库包含不安全的文件路径")
    return member, file_path
