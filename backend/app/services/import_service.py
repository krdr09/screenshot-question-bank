from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from uuid import uuid4

from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Category, Course, ImportBatch, SourceImage
from app.schemas.imports import ImportJobCreate
from app.services.folder_mapping import ImportValidationError, normalize_relative_path
from app.services.import_batch_lock import import_batch_lock


FORMAT_BY_EXTENSION = {
    ".png": ("PNG", "image/png"),
    ".jpg": ("JPEG", "image/jpeg"),
    ".jpeg": ("JPEG", "image/jpeg"),
    ".webp": ("WEBP", "image/webp"),
}


class ImportConflictError(ValueError):
    pass


@dataclass(frozen=True)
class UploadCandidate:
    original_name: str
    source_relative_path: str
    content: bytes


@dataclass(frozen=True)
class ValidatedImage:
    candidate: UploadCandidate
    sha256: str
    extension: str
    mime_type: str
    width: int
    height: int


@dataclass(frozen=True)
class ImportImagesResult:
    images: list[SourceImage]
    uploaded_count: int
    duplicate_count: int


def create_import_job(session: Session, payload: ImportJobCreate) -> ImportBatch:
    course_name = payload.course_name.strip()
    category_path = payload.category_path.strip().strip("/")
    batch_name = payload.batch_name.strip()
    source_path = normalize_relative_path(payload.source_relative_path)

    course = session.scalar(select(Course).where(Course.name == course_name))
    if course is None:
        course = Course(name=course_name)
        session.add(course)
        session.flush()

    category = None
    if category_path:
        category = session.scalar(
            select(Category).where(
                Category.course_id == course.id, Category.path == category_path
            )
        )
        if category is None:
            category = Category(course_id=course.id, path=category_path)
            session.add(category)
            session.flush()

    batch = ImportBatch(
        course_id=course.id,
        category_id=category.id if category else None,
        name=batch_name,
        source_relative_path=source_path,
    )
    session.add(batch)
    session.commit()
    return batch


def validate_candidate(candidate: UploadCandidate, settings: Settings) -> ValidatedImage:
    source_path = normalize_relative_path(candidate.source_relative_path)
    extension = PurePosixPath(candidate.original_name).suffix.lower()
    expected = FORMAT_BY_EXTENSION.get(extension)
    if expected is None:
        raise ImportValidationError("仅支持 PNG、JPG、JPEG 和 WEBP 图片")
    if len(candidate.content) > settings.max_single_file_bytes:
        raise ImportValidationError("单个图片文件超过大小限制")
    try:
        with Image.open(BytesIO(candidate.content)) as opened:
            actual_format = opened.format
            width, height = opened.size
            opened.verify()
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise ImportValidationError("文件内容不是有效图片") from error
    if actual_format != expected[0]:
        raise ImportValidationError("图片扩展名与实际格式不一致")
    if width <= 0 or height <= 0:
        raise ImportValidationError("图片尺寸无效")
    return ValidatedImage(
        candidate=UploadCandidate(
            candidate.original_name, source_path, candidate.content
        ),
        sha256=hashlib.sha256(candidate.content).hexdigest(),
        extension=extension,
        mime_type=expected[1],
        width=width,
        height=height,
    )


def import_images(
    session: Session,
    settings: Settings,
    batch: ImportBatch,
    candidates: list[UploadCandidate],
) -> ImportImagesResult:
    if not candidates:
        raise ImportValidationError("没有收到图片文件")
    validated = [validate_candidate(candidate, settings) for candidate in candidates]
    with import_batch_lock(batch.id):
        return _import_validated_images(session, settings, batch, validated)


def _import_validated_images(
    session: Session,
    settings: Settings,
    batch: ImportBatch,
    validated: list[ValidatedImage],
) -> ImportImagesResult:
    session.refresh(batch)
    if batch.status not in {"created", "uploading"}:
        raise ImportConflictError(
            f"导入任务当前状态不允许上传图片：{batch.status}"
        )

    validated_by_path: dict[str, ValidatedImage] = {}
    request_paths: list[str] = []
    for item in validated:
        path = item.candidate.source_relative_path
        previous = validated_by_path.get(path)
        if previous is not None and previous.sha256 != item.sha256:
            raise ImportConflictError("同一来源相对路径包含不同图片内容")
        validated_by_path.setdefault(path, item)
        request_paths.append(path)

    existing_by_path = {
        image.source_relative_path: image
        for image in session.scalars(
            select(SourceImage).where(
                SourceImage.batch_id == batch.id,
                SourceImage.source_relative_path.in_(list(validated_by_path)),
            )
        )
    }
    for path, existing in existing_by_path.items():
        if existing.sha256 != validated_by_path[path].sha256:
            raise ImportConflictError("来源相对路径已存在且内容不同")

    created_candidates = [
        item
        for path, item in validated_by_path.items()
        if path not in existing_by_path
    ]
    if len(created_candidates) + batch.uploaded_count > settings.max_import_files:
        raise ImportValidationError("单个导入批次最多支持 200 张图片")
    incoming_bytes = sum(len(item.candidate.content) for item in created_candidates)
    persisted_bytes = session.scalar(
        select(func.coalesce(func.sum(SourceImage.file_size), 0)).where(
            SourceImage.batch_id == batch.id
        )
    )
    if int(persisted_bytes or 0) + incoming_bytes > settings.max_import_bytes:
        raise ImportValidationError("导入图片总大小超过限制")

    temporary_paths: list[Path] = []
    written_paths: list[Path] = []
    created_by_path: dict[str, SourceImage] = {}
    known_by_hash: dict[str, SourceImage] = {}
    try:
        for offset, item in enumerate(created_candidates):
            duplicate = known_by_hash.get(item.sha256)
            if duplicate is None:
                duplicate = session.scalar(
                    select(SourceImage)
                    .where(SourceImage.sha256 == item.sha256)
                    .order_by(SourceImage.created_at, SourceImage.id)
                    .limit(1)
                )
            if duplicate is None:
                safe_name = f"{uuid4()}{item.extension}"
                relative_path = PurePosixPath("imports", batch.id, safe_name).as_posix()
                target = settings.data_dir / Path(relative_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(target.suffix + ".tmp")
                temporary_paths.append(temporary)
                temporary.write_bytes(item.candidate.content)
                os.replace(temporary, target)
                written_paths.append(target)
                duplicate_of_id = None
            else:
                safe_name = duplicate.safe_name
                relative_path = duplicate.relative_path
                duplicate_of_id = duplicate.id

            image = SourceImage(
                batch_id=batch.id,
                source_relative_path=item.candidate.source_relative_path,
                original_name=PurePosixPath(item.candidate.original_name).name,
                safe_name=safe_name,
                relative_path=relative_path,
                sha256=item.sha256,
                mime_type=item.mime_type,
                width=item.width,
                height=item.height,
                file_size=len(item.candidate.content),
                duplicate_of_id=duplicate_of_id,
                order_index=batch.uploaded_count + offset,
            )
            session.add(image)
            session.flush()
            created_by_path[item.candidate.source_relative_path] = image
            known_by_hash.setdefault(item.sha256, image)

        batch.uploaded_count += len(created_by_path)
        batch.duplicate_count += sum(
            1 for image in created_by_path.values() if image.duplicate_of_id
        )
        batch.status = "uploading"
        session.commit()
    except IntegrityError as error:
        session.rollback()
        for path in [*temporary_paths, *written_paths]:
            path.unlink(missing_ok=True)
        raise ImportConflictError("导入图片发生并发冲突，请刷新后重试") from error
    except Exception:
        session.rollback()
        for path in [*temporary_paths, *written_paths]:
            path.unlink(missing_ok=True)
        raise
    resolved_by_path = {**existing_by_path, **created_by_path}
    response_images = [resolved_by_path[path] for path in request_paths]
    return ImportImagesResult(
        images=response_images,
        uploaded_count=len(created_by_path),
        duplicate_count=sum(
            1 for image in created_by_path.values() if image.duplicate_of_id
        ),
    )

