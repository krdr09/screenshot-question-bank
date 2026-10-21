from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile, ZipInfo

from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError

from app import models  # noqa: F401
from app.config import Settings
from app.database import Base
from app.portable_settings import validate_portable_settings_bytes
from app.schemas.backups import BackupManifest
from app.services.backups.errors import BackupValidationError
from app.services.backups.manifest import database_counts, file_kind, hash_file
from app.services.backups.payload_validation import validate_database_payload


logger = logging.getLogger(__name__)
BACKEND_ROOT = Path(__file__).resolve().parents[3]
INVARIANT_TABLES = {"courses", "categories", "import_batches", "source_images"}


@dataclass(frozen=True)
class ValidatedBackup:
    manifest: BackupManifest
    staging_dir: Path
    filename: str
    migration_required: bool
    warnings: tuple[str, ...]


class ArchiveValidator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def validate(self, archive_path: Path, staging_dir: Path) -> ValidatedBackup:
        if not archive_path.is_file():
            raise BackupValidationError("备份文件不存在或无法读取")
        if archive_path.stat().st_size > self.settings.max_backup_upload_bytes:
            raise BackupValidationError("备份文件超过允许大小")
        if staging_dir.exists():
            raise BackupValidationError("恢复暂存目录冲突，请重新验证")
        try:
            with ZipFile(archive_path) as bundle:
                infos = bundle.infolist()
                canonical = self._preflight(infos)
                staging_dir.mkdir(parents=True)
                measured = self._extract(bundle, canonical, staging_dir)
            manifest = self._validate_manifest(staging_dir, measured)
            payload_root = staging_dir / "payload"
            migration_required = self.validate_staged_payload(
                database_path=payload_root / "app.db",
                manifest=manifest,
                payload_root=payload_root,
            )
            warnings = (
                ("恢复后启动时将升级数据库结构",)
                if migration_required
                else ()
            )
            return ValidatedBackup(
                manifest=manifest,
                staging_dir=staging_dir,
                filename=archive_path.name,
                migration_required=migration_required,
                warnings=warnings,
            )
        except BackupValidationError:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise
        except (
            BadZipFile,
            OSError,
            RuntimeError,
            sqlite3.DatabaseError,
            ValidationError,
        ) as error:
            shutil.rmtree(staging_dir, ignore_errors=True)
            logger.exception("备份验证失败")
            raise BackupValidationError("备份文件已损坏或格式不正确") from error

    def validate_staged_payload(
        self,
        database_path: Path,
        manifest: BackupManifest,
        payload_root: Path,
    ) -> bool:
        self._validate_portable_settings(payload_root)
        return self._validate_database(
            database_path,
            manifest,
            payload_root,
        )

    def revalidate(self, validated: ValidatedBackup) -> None:
        measured: dict[str, tuple[int, str]] = {}
        for path in sorted(validated.staging_dir.rglob("*")):
            if path.is_symlink():
                raise BackupValidationError("恢复暂存内容包含不安全的链接")
            if not path.is_file():
                continue
            relative = path.relative_to(validated.staging_dir).as_posix()
            measured[relative] = hash_file(path)
        manifest = self._validate_manifest(validated.staging_dir, measured)
        if manifest != validated.manifest:
            raise BackupValidationError("恢复暂存内容与已验证备份不一致")
        payload_root = validated.staging_dir / "payload"
        migration_required = self.validate_staged_payload(
            database_path=payload_root / "app.db",
            manifest=manifest,
            payload_root=payload_root,
        )
        if migration_required != validated.migration_required:
            raise BackupValidationError("恢复暂存数据库兼容状态已改变")

    def validate_installed(
        self, database_path: Path, validated: ValidatedBackup
    ) -> None:
        migration_required = self._validate_database(
            database_path,
            validated.manifest,
            self.settings.data_dir,
        )
        if migration_required != validated.migration_required:
            raise BackupValidationError("恢复后的数据库兼容状态不一致")

    def _preflight(self, infos: list[ZipInfo]) -> list[tuple[ZipInfo, str]]:
        if not infos or len(infos) > self.settings.max_backup_entries:
            raise BackupValidationError("备份文件数量超过允许范围")
        seen: set[str] = set()
        total = 0
        result: list[tuple[ZipInfo, str]] = []
        for info in infos:
            canonical = self._safe_member_name(info)
            if canonical in seen:
                raise BackupValidationError("备份中包含重复文件")
            seen.add(canonical)
            if info.file_size > self.settings.max_backup_member_bytes:
                raise BackupValidationError("备份中单个文件超过允许大小")
            total += info.file_size
            if total > self.settings.max_backup_uncompressed_bytes:
                raise BackupValidationError("备份解压后的总大小超过允许范围")
            if not info.is_dir() and info.file_size:
                if info.compress_size == 0:
                    raise BackupValidationError("备份压缩比异常")
                if (
                    info.file_size / info.compress_size
                    > self.settings.max_backup_compression_ratio
                ):
                    raise BackupValidationError("备份压缩比超过允许范围")
            result.append((info, canonical))
        return result

    def _safe_member_name(self, info: ZipInfo) -> str:
        name = info.filename
        trimmed = name[:-1] if info.is_dir() and name.endswith("/") else name
        if (
            not trimmed
            or "\\" in name
            or len(name) > self.settings.max_backup_path_length
            or name.startswith("/")
            or name.startswith("//")
            or re.match(r"^[A-Za-z]:", name)
        ):
            raise BackupValidationError("备份中包含不安全的文件路径")
        parts = trimmed.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise BackupValidationError("备份中包含不安全的文件路径")
        canonical = PurePosixPath(*parts).as_posix()
        mode = info.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise BackupValidationError("备份中包含不支持的链接或设备文件")
        if info.flag_bits & 0x1:
            raise BackupValidationError("不支持加密备份文件")
        return canonical

    def _extract(
        self,
        bundle: ZipFile,
        infos: list[tuple[ZipInfo, str]],
        staging_dir: Path,
    ) -> dict[str, tuple[int, str]]:
        root = staging_dir.resolve()
        total = 0
        measured: dict[str, tuple[int, str]] = {}
        for info, canonical in infos:
            destination = (staging_dir / Path(*PurePosixPath(canonical).parts)).resolve()
            if destination != root and root not in destination.parents:
                raise BackupValidationError("备份中包含不安全的文件路径")
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest = sha256()
            size = 0
            with bundle.open(info) as source, destination.open("wb") as target:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    total += len(chunk)
                    if size > self.settings.max_backup_member_bytes:
                        raise BackupValidationError("备份中单个文件超过允许大小")
                    if total > self.settings.max_backup_uncompressed_bytes:
                        raise BackupValidationError("备份解压后的总大小超过允许范围")
                    digest.update(chunk)
                    target.write(chunk)
            measured[canonical] = (size, digest.hexdigest())
        return measured

    @staticmethod
    def _validate_manifest(
        staging_dir: Path,
        measured: dict[str, tuple[int, str]],
    ) -> BackupManifest:
        manifest_path = staging_dir / "manifest.json"
        if "manifest.json" not in measured or not manifest_path.is_file():
            raise BackupValidationError("备份缺少 manifest.json")
        try:
            manifest = BackupManifest.model_validate_json(
                manifest_path.read_bytes()
            )
        except ValidationError as error:
            raise BackupValidationError("备份清单格式或版本不受支持") from error
        declared = {entry.path for entry in manifest.files}
        if len(declared) != len(manifest.files):
            raise BackupValidationError("备份清单包含重复文件")
        actual = set(measured) - {"manifest.json"}
        if actual != declared:
            raise BackupValidationError("备份文件清单与实际内容不一致")
        if manifest.file_count != len(manifest.files):
            raise BackupValidationError("备份文件数量与清单不一致")
        if manifest.uncompressed_bytes != sum(entry.size for entry in manifest.files):
            raise BackupValidationError("备份文件大小统计与清单不一致")
        for entry in manifest.files:
            if not entry.path.startswith("payload/"):
                raise BackupValidationError("备份清单包含无效路径")
            if measured.get(entry.path) != (entry.size, entry.sha256):
                raise BackupValidationError("备份文件已损坏或被修改")
            try:
                expected_kind = file_kind(Path(entry.path))
            except ValueError as error:
                raise BackupValidationError("备份清单包含无效文件类别") from error
            if entry.kind != expected_kind:
                raise BackupValidationError("备份清单文件类别与路径不一致")
        database_entries = [
            entry
            for entry in manifest.files
            if entry.path == "payload/app.db" and entry.kind == "database"
        ]
        if len(database_entries) != 1:
            raise BackupValidationError("备份缺少数据库文件")
        config_entries = [
            entry
            for entry in manifest.files
            if entry.path == "payload/config/migratable-settings.json"
            and entry.kind == "config"
        ]
        if len(config_entries) != 1:
            raise BackupValidationError("备份缺少可迁移配置文件")
        return manifest

    def _validate_portable_settings(self, payload_root: Path) -> None:
        path = payload_root / "config" / "migratable-settings.json"
        try:
            validate_portable_settings_bytes(type(self.settings), path.read_bytes())
        except (OSError, ValueError) as error:
            raise BackupValidationError("备份中的可迁移配置格式无效") from error

    def _validate_database(
        self,
        database_path: Path,
        manifest: BackupManifest,
        payload_root: Path,
    ) -> bool:
        revision, tables = self._inspect_database(database_path, INVARIANT_TABLES)
        if revision != manifest.schema_revision:
            raise BackupValidationError("数据库版本与备份清单不一致")
        actual_counts = database_counts(
            database_path,
            available_tables=tables,
        )
        if actual_counts != manifest.counts:
            raise BackupValidationError("备份统计数量与数据库记录不一致")
        validate_database_payload(
            database_path,
            manifest,
            payload_root,
            tables,
        )
        config = Config(str(BACKEND_ROOT / "alembic.ini"))
        scripts = ScriptDirectory.from_config(config)
        head = scripts.get_current_head()
        known = {script.revision for script in scripts.walk_revisions()}
        if revision not in known or head is None:
            raise BackupValidationError("备份数据库版本不兼容")
        if revision == head:
            required = set(Base.metadata.tables)
            if not required <= tables:
                raise BackupValidationError("备份数据库缺少必要数据表")
            return False
        self._prove_migration(database_path, head)
        return True

    def _prove_migration(self, database_path: Path, head: str) -> None:
        clone = database_path.parent / ".migration-validation.db"
        shutil.copy2(database_path, clone)
        try:
            environment = os.environ.copy()
            environment["QUESTION_PRACTICE_DATABASE_URL"] = (
                f"sqlite:///{clone.as_posix()}"
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "alembic",
                    "-c",
                    "alembic.ini",
                    "upgrade",
                    head,
                ],
                cwd=BACKEND_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if completed.returncode != 0:
                logger.error(
                    "备份数据库迁移验证子进程失败：%s",
                    completed.stderr.strip(),
                )
                raise BackupValidationError("备份数据库无法升级到当前版本")
            revision, tables = self._inspect_database(
                clone, set(Base.metadata.tables)
            )
            if revision != head or not set(Base.metadata.tables) <= tables:
                raise BackupValidationError("备份数据库无法升级到当前版本")
        except BackupValidationError:
            raise
        except Exception as error:
            raise BackupValidationError("备份数据库无法升级到当前版本") from error
        finally:
            clone.unlink(missing_ok=True)
            clone.with_name(f"{clone.name}-wal").unlink(missing_ok=True)
            clone.with_name(f"{clone.name}-shm").unlink(missing_ok=True)

    @staticmethod
    def _inspect_database(
        database_path: Path,
        required_tables: set[str],
    ) -> tuple[str, set[str]]:
        uri = f"file:{database_path.as_posix()}?mode=ro"
        try:
            with closing(sqlite3.connect(uri, uri=True)) as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchall()
                if integrity != [("ok",)]:
                    raise BackupValidationError("备份数据库完整性检查失败")
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise BackupValidationError("备份数据库存在外键错误")
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if not required_tables <= tables:
                    raise BackupValidationError("备份数据库缺少必要数据表")
                row = connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()
        except BackupValidationError:
            raise
        except sqlite3.DatabaseError as error:
            raise BackupValidationError("备份数据库已损坏或无法读取") from error
        if row is None or not isinstance(row[0], str) or not row[0]:
            raise BackupValidationError("备份数据库缺少版本信息")
        return row[0], tables
