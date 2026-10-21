from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models import ImportBatch


logger = logging.getLogger(__name__)

_MANIFEST_NAME = "operation.json"
_MANIFEST_VERSION = 1
_STAGING_NAME = ".delete-staging"


class BatchDeletionRecoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class BatchDeletionOperation:
    operation_id: str
    batch_id: str
    relative_paths: tuple[str, ...]
    root: Path

    @property
    def files_root(self) -> Path:
        return self.root / "files"


@dataclass(frozen=True)
class BatchDeletionRecoveryReport:
    restored_operations: int = 0
    cleaned_operations: int = 0
    cleanup_pending: int = 0


class BatchDeletionJournal:
    def __init__(self, data_dir: Path) -> None:
        self.data_root = data_dir.resolve()
        self.staging_root = self.data_root / _STAGING_NAME

    def prepare(
        self,
        batch_id: str,
        relative_paths: set[str],
    ) -> BatchDeletionOperation:
        self._validate_staging_root()
        self._validate_uuid(batch_id, "批次编号")
        normalized = tuple(
            sorted(self._normalize_relative_path(path) for path in relative_paths)
        )
        if len(normalized) != len(set(normalized)):
            raise BatchDeletionRecoveryError("批次删除清单包含重复路径")

        operation_id = str(uuid4())
        operation_root = self.staging_root / operation_id
        operation_root.mkdir(parents=True, exist_ok=False)
        operation = BatchDeletionOperation(
            operation_id=operation_id,
            batch_id=batch_id,
            relative_paths=normalized,
            root=operation_root,
        )
        manifest = {
            "version": _MANIFEST_VERSION,
            "operation_id": operation_id,
            "batch_id": batch_id,
            "relative_paths": list(normalized),
        }
        temporary = operation_root / f"{_MANIFEST_NAME}.tmp"
        published = operation_root / _MANIFEST_NAME
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(
                    manifest,
                    stream,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, published)
        except BaseException:
            shutil.rmtree(operation_root, ignore_errors=True)
            self._remove_empty_staging_root()
            raise
        return operation

    def stage(self, operation: BatchDeletionOperation) -> None:
        self._validate_operation_root(operation)
        for relative_path in operation.relative_paths:
            live, staged = self._paths(operation, relative_path)
            if live.is_symlink() or (live.exists() and not live.is_file()):
                raise BatchDeletionRecoveryError("待删除的应用内部路径不是普通文件")
            if not live.is_file():
                raise BatchDeletionRecoveryError("待删除的应用内部文件在暂存前已缺失")
            if staged.exists() or staged.is_symlink():
                raise BatchDeletionRecoveryError("批次删除暂存路径发生冲突")
            staged.parent.mkdir(parents=True, exist_ok=True)
            os.replace(live, staged)

    def restore(self, operation: BatchDeletionOperation) -> None:
        self._validate_operation_root(operation)
        for relative_path in reversed(operation.relative_paths):
            live, staged = self._paths(operation, relative_path)
            if staged.is_symlink() or live.is_symlink():
                raise BatchDeletionRecoveryError("批次删除恢复路径不得是符号链接")
            if staged.exists() and live.exists():
                raise BatchDeletionRecoveryError("批次删除恢复发现活动文件与暂存文件冲突")
            if staged.exists():
                if not staged.is_file():
                    raise BatchDeletionRecoveryError("批次删除暂存内容不是普通文件")
                live.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged, live)
            elif not live.is_file():
                raise BatchDeletionRecoveryError("批次删除恢复时活动文件和暂存文件均缺失")

    def cleanup(self, operation: BatchDeletionOperation) -> bool:
        self._validate_operation_root(operation)
        try:
            if operation.files_root.is_symlink():
                raise OSError("批次删除暂存文件目录不得是符号链接")
            if operation.files_root.exists():
                if not operation.files_root.is_dir():
                    raise OSError("批次删除暂存文件路径不是目录")
                shutil.rmtree(operation.files_root)
            manifest_path = operation.root / _MANIFEST_NAME
            manifest_path.unlink(missing_ok=True)
            (operation.root / f"{_MANIFEST_NAME}.tmp").unlink(missing_ok=True)
            if operation.root.exists():
                operation.root.rmdir()
            if operation.root.exists():
                logger.warning(
                    "批次删除暂存清理未完成，操作编号：%s",
                    operation.operation_id,
                )
                return False
            self._remove_empty_staging_root()
            return True
        except OSError:
            logger.warning(
                "批次删除暂存清理失败，将在下次启动重试，操作编号：%s",
                operation.operation_id,
                exc_info=True,
            )
            return False

    def recover(self, session: Session) -> BatchDeletionRecoveryReport:
        self._validate_staging_root()
        if not self.staging_root.exists():
            return BatchDeletionRecoveryReport()

        restored = 0
        cleaned = 0
        pending = 0
        for operation_root in sorted(self.staging_root.iterdir()):
            if operation_root.is_symlink() or not operation_root.is_dir():
                raise BatchDeletionRecoveryError("批次删除暂存目录包含无效项目")
            if not (operation_root / _MANIFEST_NAME).exists():
                if self._cleanup_manifestless_operation(operation_root):
                    cleaned += 1
                else:
                    pending += 1
                continue
            operation = self._load(operation_root)
            if session.get(ImportBatch, operation.batch_id) is not None:
                self.restore(operation)
                restored += 1
            if self.cleanup(operation):
                if session.get(ImportBatch, operation.batch_id) is None:
                    cleaned += 1
            else:
                pending += 1
        return BatchDeletionRecoveryReport(
            restored_operations=restored,
            cleaned_operations=cleaned,
            cleanup_pending=pending,
        )

    def _cleanup_manifestless_operation(self, operation_root: Path) -> bool:
        temporary_name = f"{_MANIFEST_NAME}.tmp"
        for path in operation_root.rglob("*"):
            if path.is_symlink():
                raise BatchDeletionRecoveryError(
                    "无恢复清单的批次删除目录包含符号链接"
                )
            if path.is_file() and path.name != temporary_name:
                raise BatchDeletionRecoveryError(
                    "无恢复清单的批次删除目录仍包含暂存文件"
                )
        try:
            shutil.rmtree(operation_root)
            self._remove_empty_staging_root()
            return not operation_root.exists()
        except OSError:
            logger.warning(
                "空的批次删除暂存目录清理失败，将在下次启动重试，目录编号：%s",
                operation_root.name,
                exc_info=True,
            )
            return False

    def _load(self, operation_root: Path) -> BatchDeletionOperation:
        manifest_path = operation_root / _MANIFEST_NAME
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise BatchDeletionRecoveryError("批次删除恢复清单不存在或无效")
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise BatchDeletionRecoveryError("无法读取批次删除恢复清单") from error
        if not isinstance(payload, dict) or set(payload) != {
            "version",
            "operation_id",
            "batch_id",
            "relative_paths",
        }:
            raise BatchDeletionRecoveryError("批次删除恢复清单结构无效")
        if payload["version"] != _MANIFEST_VERSION:
            raise BatchDeletionRecoveryError("批次删除恢复清单版本不受支持")
        operation_id = payload["operation_id"]
        batch_id = payload["batch_id"]
        relative_paths = payload["relative_paths"]
        if not isinstance(operation_id, str) or operation_id != operation_root.name:
            raise BatchDeletionRecoveryError("批次删除恢复清单操作编号无效")
        self._validate_uuid(operation_id, "操作编号")
        if not isinstance(batch_id, str):
            raise BatchDeletionRecoveryError("批次删除恢复清单批次编号无效")
        self._validate_uuid(batch_id, "批次编号")
        if not isinstance(relative_paths, list) or not all(
            isinstance(path, str) for path in relative_paths
        ):
            raise BatchDeletionRecoveryError("批次删除恢复清单路径列表无效")
        normalized = tuple(
            self._normalize_relative_path(path) for path in relative_paths
        )
        if len(normalized) != len(set(normalized)):
            raise BatchDeletionRecoveryError("批次删除恢复清单包含重复路径")
        operation = BatchDeletionOperation(
            operation_id=operation_id,
            batch_id=batch_id,
            relative_paths=normalized,
            root=operation_root,
        )
        self._validate_operation_root(operation)
        return operation

    def _paths(
        self,
        operation: BatchDeletionOperation,
        relative_path: str,
    ) -> tuple[Path, Path]:
        pure_path = PurePosixPath(self._normalize_relative_path(relative_path))
        path_parts = Path(*pure_path.parts)
        live = (self.data_root / path_parts).resolve()
        files_root = operation.files_root.resolve()
        staged = (files_root / path_parts).resolve()
        if self.data_root not in live.parents:
            raise BatchDeletionRecoveryError("批次删除活动文件路径越界")
        if files_root not in staged.parents:
            raise BatchDeletionRecoveryError("批次删除暂存文件路径越界")
        return live, staged

    def _validate_operation_root(
        self,
        operation: BatchDeletionOperation,
    ) -> None:
        self._validate_staging_root()
        resolved_staging = self.staging_root.resolve()
        resolved_operation = operation.root.resolve()
        if (
            operation.root.is_symlink()
            or resolved_operation.parent != resolved_staging
            or resolved_operation.name != operation.operation_id
        ):
            raise BatchDeletionRecoveryError("批次删除操作目录越界")

    def _validate_staging_root(self) -> None:
        expected = self.data_root / _STAGING_NAME
        resolved = self.staging_root.resolve()
        if (
            resolved != expected
            or self.staging_root.is_symlink()
            or (self.staging_root.exists() and not self.staging_root.is_dir())
        ):
            raise BatchDeletionRecoveryError("批次删除暂存根目录无效或越界")

    @staticmethod
    def _normalize_relative_path(value: str) -> str:
        if not isinstance(value, str) or not value or "\\" in value:
            raise BatchDeletionRecoveryError("批次删除清单路径无效")
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.parts[0] == _STAGING_NAME
            or path.as_posix() != value
        ):
            raise BatchDeletionRecoveryError("批次删除清单路径无效或越界")
        return path.as_posix()

    @staticmethod
    def _validate_uuid(value: str, label: str) -> None:
        try:
            parsed = UUID(value)
        except (TypeError, ValueError) as error:
            raise BatchDeletionRecoveryError(f"批次删除恢复清单{label}无效") from error
        if str(parsed) != value:
            raise BatchDeletionRecoveryError(f"批次删除恢复清单{label}无效")

    def _remove_empty_staging_root(self) -> None:
        try:
            self.staging_root.rmdir()
        except OSError:
            pass
