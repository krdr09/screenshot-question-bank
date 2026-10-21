from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class FolderFile(BaseModel):
    relative_path: str
    size: int = Field(ge=0)


class FolderPreviewRequest(BaseModel):
    root_name: str = Field(min_length=1, max_length=200)
    files: list[FolderFile] = Field(min_length=1, max_length=200)
    flat_course_name: str | None = Field(default=None, max_length=200)
    flat_batch_name: str | None = Field(default=None, max_length=200)


class FolderMapping(BaseModel):
    course_name: str
    category_path: str
    batch_name: str
    source_relative_path: str
    file_count: int


class FolderPreviewResponse(BaseModel):
    mappings: list[FolderMapping]
    image_count: int


class ImportJobCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    course_name: str = Field(min_length=1, max_length=200)
    category_path: str = Field(default="", max_length=500)
    batch_name: str = Field(min_length=1, max_length=200)
    source_relative_path: str = Field(min_length=1, max_length=1000)


class SourceImageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source_relative_path: str
    original_name: str
    relative_path: str
    sha256: str
    mime_type: str
    width: int
    height: int
    file_size: int
    duplicate_of_id: str | None
    order_index: int
    ocr_status: str
    ocr_text: str | None
    ocr_error: str | None


class ImportJobResponse(BaseModel):
    id: str
    course_name: str
    category_path: str
    batch_name: str
    source_relative_path: str
    status: str
    uploaded_count: int
    duplicate_count: int
    processed_count: int
    found_question_count: int
    high_confidence_count: int
    review_count: int
    error_count: int
    current_file: str | None
    ocr_device: str | None
    cancel_requested: bool
    error_log: list[dict[str, str]] = Field(default_factory=list)
    created_at: datetime
    images: list[SourceImageResponse] = Field(default_factory=list)


class UploadResponse(BaseModel):
    uploaded_count: int
    duplicate_count: int
    images: list[SourceImageResponse]
