from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


BackupFileKind = Literal["database", "import", "crop", "ocr", "config"]


class BackupFileEntry(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    kind: BackupFileKind
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BackupCounts(BaseModel):
    questions: int = Field(default=0, ge=0)
    source_images: int = Field(default=0, ge=0)
    crop_images: int = Field(default=0, ge=0)
    attempts: int = Field(default=0, ge=0)
    wrong_questions: int = Field(default=0, ge=0)


class BackupManifest(BaseModel):
    backup_format_version: Literal[1] = 1
    backup_id: UUID
    app_version: str
    schema_revision: str
    created_at: datetime
    reason: Literal["manual", "pre_restore"] = "manual"
    file_count: int = Field(ge=1)
    uncompressed_bytes: int = Field(ge=1)
    counts: BackupCounts
    files: list[BackupFileEntry] = Field(min_length=1)


class BackupDetail(BaseModel):
    backup_id: UUID
    filename: str
    created_at: datetime
    size: int = Field(ge=0)
    app_version: str
    schema_revision: str
    reason: Literal["manual", "pre_restore"]
    counts: BackupCounts


class BackupListResponse(BaseModel):
    items: list[BackupDetail] = Field(default_factory=list)


class BackupValidationResult(BaseModel):
    validation_token: str
    backup_id: UUID
    filename: str
    created_at: datetime
    app_version: str
    schema_revision: str
    file_count: int = Field(ge=1)
    uncompressed_bytes: int = Field(ge=1)
    counts: BackupCounts
    hashes_verified: Literal[True] = True
    migration_required: bool
    warnings: list[str] = Field(default_factory=list)


class BackupRestoreRequest(BaseModel):
    validation_token: str = Field(min_length=32, max_length=256)
    confirmed: Literal[True]


class BackupRestoreResult(BaseModel):
    restored_backup_id: UUID
    safety_backup: BackupDetail
    restart_required: Literal[True] = True
