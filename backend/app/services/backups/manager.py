from __future__ import annotations

import shutil
from base64 import urlsafe_b64encode
from dataclasses import replace
from pathlib import Path
from typing import Callable
from uuid import uuid4

from fastapi import UploadFile

from app.config import Settings
from app.database import Database
from app.schemas.backups import (
    BackupDetail,
    BackupListResponse,
    BackupRestoreRequest,
    BackupRestoreResult,
    BackupValidationResult,
)
from app.services.backups.archive import BackupArchiveService
from app.services.backups.errors import BackupValidationError
from app.services.backups.maintenance import MaintenanceCoordinator
from app.services.backups.recovery import (
    RestoreRecoverySentinel,
    RestoreRecoveryState,
)
from app.services.backups.restore import FileOperations, RestoreCoordinator, WorkerControl
from app.services.backups.tokens import RestoreTokenRegistry
from app.services.backups.validation import ArchiveValidator, ValidatedBackup


class BackupManager:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        worker: WorkerControl,
        maintenance: MaintenanceCoordinator,
        mark_restart_required: Callable[[], None],
        recovery_sentinel: RestoreRecoverySentinel,
        mark_recovery_required: Callable[[RestoreRecoveryState], None],
    ) -> None:
        self.settings = settings
        self.maintenance = maintenance
        self.archives = BackupArchiveService(database, settings)
        self.validator = ArchiveValidator(settings)
        self.tokens = RestoreTokenRegistry(settings.backup_token_ttl_seconds)
        self.recovery_sentinel = recovery_sentinel
        self.staging_root = settings.data_dir / ".restore-staging"
        self.upload_root = settings.data_dir / ".restore-uploads"
        self.rollback_roots = {
            settings.data_dir / ".restore-rollback",
            database.sqlite_path().parent / ".restore-rollback",
        }
        self.restorer = RestoreCoordinator(
            database=database,
            settings=settings,
            worker=worker,
            archives=self.archives,
            validator=self.validator,
            filesystem=FileOperations(),
            mark_restart_required=mark_restart_required,
            recovery_sentinel=self.recovery_sentinel,
            mark_recovery_required=mark_recovery_required,
        )

    def pending_recovery(self) -> RestoreRecoveryState | None:
        return self.recovery_sentinel.load()

    def start_token_cleanup(self) -> None:
        self.tokens.start_cleanup()

    def stop_token_cleanup(self) -> None:
        self.tokens.stop_cleanup()

    def cleanup_startup(self) -> None:
        shutil.rmtree(self.staging_root, ignore_errors=True)
        shutil.rmtree(self.upload_root, ignore_errors=True)
        for root in self.rollback_roots:
            if not root.is_dir() or root.is_symlink():
                continue
            for operation in root.iterdir():
                if (
                    operation.is_dir()
                    and not operation.is_symlink()
                    and (operation / ".committed").is_file()
                ):
                    shutil.rmtree(operation, ignore_errors=True)
            try:
                root.rmdir()
            except OSError:
                pass

    def create_backup(self) -> BackupDetail:
        with self.maintenance.exclusive("创建备份"):
            return self.archives.create()

    def list_backups(self) -> BackupListResponse:
        return BackupListResponse(items=self.archives.list())

    def resolve_download(self, backup_id: str) -> Path:
        return self.archives.resolve(backup_id)

    def validate_existing(self, backup_id: str) -> BackupValidationResult:
        archive = self.archives.resolve(backup_id)
        validated = self.validator.validate(archive, self._next_staging_dir())
        return self._issue(validated)

    async def validate_upload(self, upload: UploadFile) -> BackupValidationResult:
        operation = self.upload_root / str(uuid4())
        archive = operation / "upload.zip"
        operation.mkdir(parents=True)
        size = 0
        try:
            with archive.open("wb") as target:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > self.settings.max_backup_upload_bytes:
                        raise BackupValidationError("备份文件超过允许大小")
                    target.write(chunk)
            validated = self.validator.validate(archive, self._next_staging_dir())
            safe_name = Path(upload.filename or "external-backup.zip").name
            return self._issue(replace(validated, filename=safe_name))
        finally:
            await upload.close()
            shutil.rmtree(operation, ignore_errors=True)

    def restore(self, request: BackupRestoreRequest) -> BackupRestoreResult:
        with self.maintenance.exclusive("恢复备份"):
            validated = self.tokens.consume(request.validation_token)
            try:
                return self.restorer.restore(validated)
            except Exception:
                self.tokens.discard_validated(validated)
                raise

    def _next_staging_dir(self) -> Path:
        operation_token = (
            urlsafe_b64encode(uuid4().bytes).decode("ascii").rstrip("=")
        )
        return self.staging_root / operation_token

    def _issue(self, validated: ValidatedBackup) -> BackupValidationResult:
        token = self.tokens.issue(validated)
        manifest = validated.manifest
        return BackupValidationResult(
            validation_token=token,
            backup_id=manifest.backup_id,
            filename=validated.filename,
            created_at=manifest.created_at,
            app_version=manifest.app_version,
            schema_revision=manifest.schema_revision,
            file_count=manifest.file_count,
            uncompressed_bytes=manifest.uncompressed_bytes,
            counts=manifest.counts,
            hashes_verified=True,
            migration_required=validated.migration_required,
            warnings=list(validated.warnings),
        )
