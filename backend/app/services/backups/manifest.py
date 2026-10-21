from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path

from app.config import Settings
from app.portable_settings import portable_settings_payload
from app.schemas.backups import BackupCounts, BackupFileEntry, BackupFileKind


COUNT_TABLES = {
    "questions": "question_candidates",
    "source_images": "source_images",
    "crop_images": "candidate_image_parts",
    "attempts": "attempts",
    "wrong_questions": "wrong_book_entries",
}


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def hash_file(path: Path) -> tuple[int, str]:
    digest = sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def schema_revision(snapshot: Path) -> str:
    with closing(sqlite3.connect(snapshot)) as connection:
        row = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
    if row is None or not isinstance(row[0], str) or not row[0]:
        raise ValueError("数据库缺少版本信息，无法创建备份")
    return row[0]


def database_counts(
    snapshot: Path,
    available_tables: set[str] | None = None,
) -> BackupCounts:
    values: dict[str, int] = {}
    with closing(sqlite3.connect(snapshot)) as connection:
        for field, table in COUNT_TABLES.items():
            if available_tables is not None and table not in available_tables:
                values[field] = 0
                continue
            values[field] = int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
    return BackupCounts(**values)


def export_ocr_json(snapshot: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(snapshot)) as connection:
        rows = connection.execute(
            "SELECT id, ocr_result_json FROM source_images "
            "WHERE ocr_result_json IS NOT NULL ORDER BY id"
        )
        for source_id, raw_json in rows:
            value = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
            (destination / f"{source_id}.json").write_bytes(
                canonical_json_bytes(value)
            )


def export_portable_settings(settings: Settings, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(canonical_json_bytes(portable_settings_payload(settings)))


def file_kind(relative_path: Path) -> BackupFileKind:
    parts = relative_path.parts
    if parts == ("payload", "app.db"):
        return "database"
    if len(parts) < 2 or parts[0] != "payload":
        raise ValueError("备份暂存文件不在 payload 目录")
    mapping: dict[str, BackupFileKind] = {
        "imports": "import",
        "crops": "crop",
        "ocr": "ocr",
        "config": "config",
    }
    try:
        return mapping[parts[1]]
    except KeyError as error:
        raise ValueError("备份暂存文件类别无效") from error


def inventory(payload_root: Path) -> list[tuple[BackupFileEntry, Path]]:
    operation_root = payload_root.parent
    entries: list[tuple[BackupFileEntry, Path]] = []
    for source in sorted(path for path in payload_root.rglob("*") if path.is_file()):
        relative = source.relative_to(operation_root)
        size, digest = hash_file(source)
        entries.append(
            (
                BackupFileEntry(
                    path=relative.as_posix(),
                    kind=file_kind(relative),
                    size=size,
                    sha256=digest,
                ),
                source,
            )
        )
    return entries
