from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.cropping.questions import CropBounds, crop_question_image


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CropReplacement:
    source: Path
    target: Path
    bounds: CropBounds


@dataclass
class _StagedReplacement:
    staged: Path
    target: Path
    backup: Path | None = None
    installed: bool = False


def _temporary_sibling(target: Path, role: str) -> Path:
    token = uuid4().hex
    candidate = target.with_name(f".{target.name}.{role}-{token}")
    if len(os.path.abspath(candidate)) >= 260:
        return target.with_name(f".{role}-{token}")
    return candidate


class CropFileTransaction:
    def __init__(self, staged: list[_StagedReplacement]) -> None:
        self._staged = staged

    @classmethod
    def stage(cls, replacements: list[CropReplacement]) -> "CropFileTransaction":
        staged_replacements: list[_StagedReplacement] = []
        try:
            for replacement in replacements:
                replacement.target.parent.mkdir(parents=True, exist_ok=True)
                staged = _temporary_sibling(replacement.target, "recrop-stage")
                item = _StagedReplacement(
                    staged=staged,
                    target=replacement.target,
                )
                staged_replacements.append(item)
                crop_question_image(replacement.source, staged, replacement.bounds)
        except Exception:
            for item in staged_replacements:
                try:
                    item.staged.unlink(missing_ok=True)
                except Exception:
                    logger.warning(
                        "裁切暂存失败后的临时文件清理失败: %s",
                        item.staged,
                        exc_info=True,
                    )
            raise
        return cls(staged_replacements)

    def install(self) -> None:
        for item in self._staged:
            if item.target.exists():
                item.backup = _temporary_sibling(item.target, "recrop-backup")
                os.replace(item.target, item.backup)
            os.replace(item.staged, item.target)
            item.installed = True

    def rollback(self) -> None:
        for item in reversed(self._staged):
            if item.backup is not None:
                try:
                    if item.backup.exists():
                        os.replace(item.backup, item.target)
                except Exception:
                    logger.warning(
                        "裁切文件回滚时恢复备份失败: %s -> %s",
                        item.backup,
                        item.target,
                        exc_info=True,
                    )
            elif item.installed:
                try:
                    item.target.unlink(missing_ok=True)
                except Exception:
                    logger.warning(
                        "裁切文件回滚时移除新文件失败: %s",
                        item.target,
                        exc_info=True,
                    )
            try:
                item.staged.unlink(missing_ok=True)
            except Exception:
                logger.warning(
                    "裁切文件回滚时清理临时文件失败: %s",
                    item.staged,
                    exc_info=True,
                )

    def finalize(self) -> None:
        for item in self._staged:
            try:
                item.staged.unlink(missing_ok=True)
            except Exception:
                logger.warning(
                    "裁切提交后清理临时文件失败: %s",
                    item.staged,
                    exc_info=True,
                )
            if item.backup is not None:
                try:
                    item.backup.unlink(missing_ok=True)
                except Exception:
                    logger.warning(
                        "裁切提交后清理备份文件失败: %s",
                        item.backup,
                        exc_info=True,
                    )
