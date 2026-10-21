from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from app.config import Settings
from app.database import Database
from app.schemas.backups import BackupDetail, BackupManifest
from app.services.backups.errors import (
    BackupInternalError,
    BackupNotFoundError,
    BackupValidationError,
)
from app.services.backups.manifest import (
    database_counts,
    export_ocr_json,
    export_portable_settings,
    inventory,
    schema_revision,
)
from app.services.backups.validation import ArchiveValidator


logger = logging.getLogger(__name__)


class BackupArchiveService:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings
        self.backups_dir = settings.data_dir / "backups"
        self.staging_dir = settings.data_dir / ".b"
        self.validator = ArchiveValidator(settings)

    def create(
        self, reason: Literal["manual", "pre_restore"] = "manual"
    ) -> BackupDetail:
        operation_id = uuid4()
        operation_root = self._create_operation_root()
        payload_root = operation_root / "payload"
        snapshot = payload_root / "app.db"
        temporary_archive = self.backups_dir / f".{operation_id}.tmp"
        created_at = datetime.now().astimezone()
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._online_snapshot(snapshot)
            self._copy_managed_tree("imports", payload_root / "imports")
            self._copy_managed_tree("crops", payload_root / "crops")
            export_ocr_json(snapshot, payload_root / "ocr")
            export_portable_settings(
                self.settings,
                payload_root / "config" / "migratable-settings.json",
            )
            sources = inventory(payload_root)
            manifest = BackupManifest(
                backup_id=operation_id,
                app_version=self.settings.app_version,
                schema_revision=schema_revision(snapshot),
                created_at=created_at.astimezone(timezone.utc),
                reason=reason,
                file_count=len(sources),
                uncompressed_bytes=sum(entry.size for entry, _ in sources),
                counts=database_counts(snapshot),
                files=[entry for entry, _ in sources],
            )
            self._validate_staged_payload(
                snapshot=snapshot,
                manifest=manifest,
                payload_root=payload_root,
            )
            self._write_archive(temporary_archive, manifest, sources)
            self._verify_created_archive(temporary_archive, manifest)
            final = self._available_path(created_at)
            os.replace(temporary_archive, final)
            return self._detail(final, manifest)
        finally:
            temporary_archive.unlink(missing_ok=True)
            shutil.rmtree(operation_root, ignore_errors=True)
            self._remove_empty_staging_root()

    def list(self) -> list[BackupDetail]:
        if not self.backups_dir.is_dir():
            return []
        details: list[BackupDetail] = []
        for archive in self.backups_dir.glob("question-practice-backup-*.zip"):
            try:
                manifest = self._read_manifest(archive)
                details.append(self._detail(archive, manifest))
            except Exception:
                logger.warning("忽略无法读取的本地备份条目", exc_info=True)
        return sorted(details, key=lambda item: item.created_at, reverse=True)

    def resolve(self, backup_id: str) -> Path:
        try:
            expected = UUID(backup_id)
        except ValueError as error:
            raise BackupNotFoundError() from error
        for detail in self.list():
            if detail.backup_id == expected:
                path = (self.backups_dir / detail.filename).resolve()
                if path.parent != self.backups_dir.resolve() or not path.is_file():
                    break
                return path
        raise BackupNotFoundError()

    def _create_operation_root(self) -> Path:
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        for _ in range(32):
            operation_root = self.staging_dir / uuid4().hex[:12]
            try:
                operation_root.mkdir()
            except FileExistsError:
                continue
            return operation_root
        raise BackupInternalError("无法创建唯一的备份暂存目录，请重试")

    def _online_snapshot(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with (
            closing(sqlite3.connect(self.database.sqlite_path())) as source,
            closing(sqlite3.connect(destination)) as target,
        ):
            source.backup(target)

    def _validate_staged_payload(
        self,
        *,
        snapshot: Path,
        manifest: BackupManifest,
        payload_root: Path,
    ) -> None:
        try:
            self.validator.validate_staged_payload(
                database_path=snapshot,
                manifest=manifest,
                payload_root=payload_root,
            )
        except BackupValidationError as error:
            logger.exception("备份创建暂存载荷语义校验失败")
            raise BackupInternalError(
                "备份创建期间检测到数据不一致，未生成备份；"
                "请关闭可能修改数据目录的程序后重试"
            ) from error

    def _copy_managed_tree(self, name: str, destination: Path) -> None:
        source_root = (self.settings.data_dir / name).resolve()
        if not source_root.is_dir():
            return
        data_root = self.settings.data_dir.resolve()
        for source in sorted(source_root.rglob("*")):
            if source.is_symlink():
                raise ValueError("应用数据目录包含不支持的链接")
            if not source.is_file():
                continue
            resolved = source.resolve()
            if data_root not in resolved.parents:
                raise ValueError("应用内部文件路径无效")
            target = destination / source.relative_to(source_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    @staticmethod
    def _write_archive(archive, manifest, sources) -> None:
        with ZipFile(archive, "w", compression=ZIP_DEFLATED, allowZip64=True) as bundle:
            bundle.writestr(
                "manifest.json",
                manifest.model_dump_json(indent=2).encode("utf-8"),
            )
            for entry, source in sources:
                bundle.write(source, entry.path)

    @staticmethod
    def _verify_created_archive(archive: Path, expected: BackupManifest) -> None:
        with ZipFile(archive) as bundle:
            infos = bundle.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise ValueError("新备份包含重复文件")
            actual = set(names) - {"manifest.json"}
            declared = {entry.path for entry in expected.files}
            if actual != declared:
                raise ValueError("新备份文件清单不一致")
            parsed = BackupManifest.model_validate_json(bundle.read("manifest.json"))
            if parsed != expected:
                raise ValueError("新备份清单写入不一致")
            for entry in expected.files:
                payload = bundle.read(entry.path)
                if len(payload) != entry.size:
                    raise ValueError("新备份文件大小不一致")
                if __import__("hashlib").sha256(payload).hexdigest() != entry.sha256:
                    raise ValueError("新备份文件哈希不一致")

    def _available_path(self, created_at: datetime) -> Path:
        stem = f"question-practice-backup-{created_at:%Y%m%d-%H%M%S}"
        candidate = self.backups_dir / f"{stem}.zip"
        suffix = 1
        while candidate.exists():
            candidate = self.backups_dir / f"{stem}-{suffix}.zip"
            suffix += 1
        return candidate

    @staticmethod
    def _read_manifest(archive: Path) -> BackupManifest:
        with ZipFile(archive) as bundle:
            return BackupManifest.model_validate_json(bundle.read("manifest.json"))

    @staticmethod
    def _detail(archive: Path, manifest: BackupManifest) -> BackupDetail:
        return BackupDetail(
            backup_id=manifest.backup_id,
            filename=archive.name,
            created_at=manifest.created_at,
            size=archive.stat().st_size,
            app_version=manifest.app_version,
            schema_revision=manifest.schema_revision,
            reason=manifest.reason,
            counts=manifest.counts,
        )

    def _remove_empty_staging_root(self) -> None:
        try:
            self.staging_dir.rmdir()
        except OSError:
            pass
