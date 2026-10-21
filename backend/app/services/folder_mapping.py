from __future__ import annotations

from collections import defaultdict
from pathlib import PurePosixPath

from app.schemas.imports import FolderMapping, FolderPreviewRequest


ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


class ImportValidationError(ValueError):
    pass


def normalize_relative_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or (path.parts and ":" in path.parts[0])
    ):
        raise ImportValidationError("文件相对路径不安全")
    return path.as_posix()


def build_folder_mappings(request: FolderPreviewRequest) -> list[FolderMapping]:
    root_name = request.root_name.strip()
    grouped: dict[tuple[str, str, str, str], int] = defaultdict(int)
    for item in request.files:
        normalized = normalize_relative_path(item.relative_path)
        parts = list(PurePosixPath(normalized).parts)
        if parts and parts[0] == root_name:
            parts = parts[1:]
        if not parts or PurePosixPath(parts[-1]).suffix.lower() not in ALLOWED_EXTENSIONS:
            continue
        directories = parts[:-1]
        if not directories:
            if not request.flat_course_name or not request.flat_batch_name:
                raise ImportValidationError("普通图片文件夹需要填写课程名和批次名")
            course = request.flat_course_name.strip()
            category = ""
            batch = request.flat_batch_name.strip()
            source = ""
        elif len(directories) == 1:
            if not request.flat_batch_name:
                raise ImportValidationError("只有一级目录时需要填写批次名")
            course = directories[0]
            category = ""
            batch = request.flat_batch_name.strip()
            source = directories[0]
        else:
            course = directories[0]
            batch = directories[-1]
            category = "/".join(directories[1:-1])
            source = "/".join(directories)
        if not course or not batch:
            raise ImportValidationError("课程名和批次名不能为空")
        grouped[(course, category, batch, source)] += 1

    if not grouped:
        raise ImportValidationError("所选文件夹中没有支持的图片")
    return [
        FolderMapping(
            course_name=course,
            category_path=category,
            batch_name=batch,
            source_relative_path=source,
            file_count=count,
        )
        for (course, category, batch, source), count in grouped.items()
    ]
