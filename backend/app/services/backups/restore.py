from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol
from uuid import uuid4

from app.config import Settings
from app.database import Database
from app.schemas.backups import BackupRestoreResult
from app.services.backups.archive import BackupArchiveService
from app.services.backups.errors import (
    BackupRecoveryRequiredError,
    BackupRestoreError,
    BackupValidationError,
)
from app.services.backups.recovery import (
    RestoreRecoverySentinel,
    RestoreRecoveryState,
    recovery_public_detail,
)
from app.services.backups.validation import ArchiveValidator, ValidatedBackup


logger = logging.getLogger(__name__)


class WorkerControl(Protocol):
    def stop(self) -> None: ...
    def start(self) -> None: ...


class FileOperations:
    def replace(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)

    def remove(self, path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)


@dataclass(frozen=True)
class SwapTarget:
    staged: Path
    live: Path
    rollback: Path


@dataclass(frozen=True)
class RollbackOutcome:
    errors: tuple[Exception, ...]
    attempted_steps: int

    @property
    def complete(self) -> bool:
        return not self.errors


class RestoreCoordinator:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        worker: WorkerControl,
        archives: BackupArchiveService,
        validator: ArchiveValidator,
        filesystem: FileOperations,
        mark_restart_required: Callable[[], None],
        recovery_sentinel: RestoreRecoverySentinel,
        mark_recovery_required: Callable[[RestoreRecoveryState], None],
    ) -> None:
        self.database = database
        self.settings = settings
        self.worker = worker
        self.archives = archives
        self.validator = validator
        self.filesystem = filesystem
        self.recovery_sentinel = recovery_sentinel
        self.mark_restart_required = mark_restart_required
        self.mark_recovery_required = mark_recovery_required

    def restore(self, validated: ValidatedBackup) -> BackupRestoreResult:
        self.validator.revalidate(validated)
        moved_live: list[SwapTarget] = []
        installed: list[SwapTarget] = []
        operation_id = str(uuid4())
        targets = self._targets(validated, operation_id)
        try:
            safety_backup = self.archives.create(reason="pre_restore")
        except Exception as error:
            logger.exception("恢复事务失败，已尝试回滚")
            raise BackupRestoreError() from error

        try:
            recovery_state = self.recovery_sentinel.arm(
                operation_id=operation_id,
                safety_backup_id=str(safety_backup.backup_id),
                safety_backup_filename=safety_backup.filename,
            )
        except OSError as error:
            logger.exception("恢复哨兵创建失败，恢复未启动")
            raise BackupRestoreError(
                "恢复无法安全启动，原有数据未被修改"
            ) from error

        worker_stopped = False
        try:
            self.worker.stop()
            worker_stopped = True
            self.database.dispose()
            for target in targets:
                if target.live.exists():
                    self.filesystem.replace(target.live, target.rollback)
                    moved_live.append(target)
            for target in targets:
                if target.staged.exists():
                    self.filesystem.replace(target.staged, target.live)
                    installed.append(target)
            self.validator.validate_installed(
                self.database.sqlite_path(), validated
            )
        except BackupValidationError as error:
            logger.exception("恢复后的数据校验失败，已回滚")
            self._raise_after_failed_restore(
                error,
                installed,
                moved_live,
                recovery_state,
                worker_stopped,
            )
        except Exception as error:
            logger.exception("恢复事务失败，已尝试回滚")
            self._raise_after_failed_restore(
                error,
                installed,
                moved_live,
                recovery_state,
                worker_stopped,
            )

        try:
            self._mark_rollback_committed(targets)
            self.mark_restart_required()
            self.recovery_sentinel.clear()
        except OSError as error:
            self.mark_recovery_required(recovery_state)
            raise BackupRecoveryRequiredError(
                recovery_public_detail(recovery_state)
            ) from error

        try:
            self._cleanup_rollback(targets)
        except OSError:
            logger.exception("恢复已完成，但旧数据回滚目录清理失败，将在下次启动时重试")
        shutil.rmtree(validated.staging_dir, ignore_errors=True)
        return BackupRestoreResult(
            restored_backup_id=validated.manifest.backup_id,
            safety_backup=safety_backup,
            restart_required=True,
        )

    def _targets(
        self, validated: ValidatedBackup, operation_id: str
    ) -> list[SwapTarget]:
        payload = validated.staging_dir / "payload"
        database_path = self.database.sqlite_path()
        database_rollback = (
            database_path.parent
            / ".restore-rollback"
            / operation_id
            / "app.db"
        )
        data_rollback = self.settings.data_dir / ".restore-rollback" / operation_id
        targets = [
            SwapTarget(payload / "app.db", database_path, database_rollback)
        ]
        targets.extend(
            SwapTarget(
                payload / name,
                self.settings.data_dir / name,
                data_rollback / name,
            )
            for name in ("imports", "crops", "ocr", "config")
        )
        return targets

    def _rollback(
        self,
        installed: list[SwapTarget],
        moved_live: list[SwapTarget],
    ) -> RollbackOutcome:
        rollback_errors: list[Exception] = []
        attempted_steps = 0
        for target in reversed(installed):
            try:
                if target.live.exists():
                    attempted_steps += 1
                    self.filesystem.remove(target.live)
            except Exception as error:
                rollback_errors.append(error)
        for target in reversed(moved_live):
            try:
                if not target.rollback.exists():
                    rollback_errors.append(
                        FileNotFoundError("restore rollback material missing")
                    )
                    continue
                attempted_steps += 1
                self.filesystem.replace(target.rollback, target.live)
            except Exception as error:
                rollback_errors.append(error)
        if rollback_errors:
            logger.critical(
                "恢复失败后的文件补偿不完整，错误数：%d",
                len(rollback_errors),
                exc_info=rollback_errors[-1],
            )
        return RollbackOutcome(tuple(rollback_errors), attempted_steps)

    def _raise_after_failed_restore(
        self,
        error: Exception,
        installed: list[SwapTarget],
        moved_live: list[SwapTarget],
        recovery_state: RestoreRecoveryState,
        worker_stopped: bool,
    ) -> None:
        outcome = self._rollback(installed, moved_live)
        if outcome.complete:
            try:
                self.recovery_sentinel.clear()
            except OSError as clear_error:
                self.mark_recovery_required(recovery_state)
                raise BackupRecoveryRequiredError(
                    recovery_public_detail(recovery_state)
                ) from clear_error
            if worker_stopped:
                self._resume_worker_after_failure()
            raise BackupRestoreError() from error

        self.mark_recovery_required(recovery_state)
        raise BackupRecoveryRequiredError(
            recovery_public_detail(recovery_state)
        ) from error

    def _cleanup_rollback(self, targets: list[SwapTarget]) -> None:
        roots = self._rollback_roots(targets)
        for root in roots:
            if root.exists():
                self.filesystem.remove(root)
        for parent in {root.parent for root in roots}:
            try:
                parent.rmdir()
            except OSError:
                pass

    def _mark_rollback_committed(self, targets: list[SwapTarget]) -> None:
        for root in self._rollback_roots(targets):
            root.mkdir(parents=True, exist_ok=True)
            (root / ".committed").write_text("committed\n", encoding="utf-8")

    @staticmethod
    def _rollback_roots(targets: list[SwapTarget]) -> set[Path]:
        return {
            target.rollback.parent
            if target.rollback.name == "app.db"
            else target.rollback.parents[0]
            for target in targets
        }

    def _resume_worker_after_failure(self) -> None:
        if not self.settings.start_background_worker:
            return
        try:
            self.worker.start()
        except Exception:
            logger.exception("恢复失败后重新启动 OCR 工作者失败")
