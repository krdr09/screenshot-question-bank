from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeVar

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from app.schemas.backups import (
    BackupDetail,
    BackupListResponse,
    BackupRestoreRequest,
    BackupRestoreResult,
    BackupValidationResult,
)
from app.services.backups.errors import BackupError, BackupInternalError
from app.services.backups.manager import BackupManager


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/backups", tags=["备份恢复"])
T = TypeVar("T")


def manager(request: Request) -> BackupManager:
    return request.app.state.backup_manager


def safe_call(operation: Callable[[], T]) -> T:
    try:
        return operation()
    except BackupError:
        raise
    except Exception as error:
        logger.exception("未预期的备份操作错误")
        raise BackupInternalError() from error


@router.post("", response_model=BackupDetail, status_code=201)
def create_backup(request: Request) -> BackupDetail:
    return safe_call(manager(request).create_backup)


@router.get("", response_model=BackupListResponse)
def list_backups(request: Request) -> BackupListResponse:
    return safe_call(manager(request).list_backups)


@router.get("/{backup_id}/content")
def download_backup(request: Request, backup_id: str) -> FileResponse:
    path = safe_call(lambda: manager(request).resolve_download(backup_id))
    return FileResponse(path, media_type="application/zip", filename=path.name)


@router.post("/validate", response_model=BackupValidationResult)
async def validate_backup(
    request: Request,
    backup_id: str | None = Form(default=None),
    file: UploadFile | None = File(default=None),
) -> BackupValidationResult:
    if (backup_id is None) == (file is None):
        raise HTTPException(
            status_code=422,
            detail="请选择一个已有备份或一个外部 ZIP 文件",
        )
    try:
        if backup_id is not None:
            return manager(request).validate_existing(backup_id)
        assert file is not None
        return await manager(request).validate_upload(file)
    except BackupError:
        raise
    except Exception as error:
        logger.exception("未预期的备份验证错误")
        raise BackupInternalError() from error


@router.post("/restore", response_model=BackupRestoreResult)
def restore_backup(
    request: Request, payload: BackupRestoreRequest
) -> BackupRestoreResult:
    return safe_call(lambda: manager(request).restore(payload))
