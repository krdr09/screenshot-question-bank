from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.database import get_session
from app.models import ImportBatch, SourceImage
from app.schemas.imports import (
    FolderPreviewRequest,
    FolderPreviewResponse,
    ImportJobCreate,
    ImportJobResponse,
    SourceImageResponse,
    UploadResponse,
)
from app.services.folder_mapping import ImportValidationError, build_folder_mappings
from app.services.import_service import (
    ImportConflictError,
    UploadCandidate,
    create_import_job,
    import_images,
)
from app.services.ocr_error_messages import (
    PUBLIC_OCR_ERROR_MESSAGE,
    public_ocr_error_message,
)


router = APIRouter(prefix="/import-jobs", tags=["导入"])


def get_job_or_404(session: Session, job_id: str) -> ImportBatch:
    batch = session.scalar(
        select(ImportBatch)
        .options(
            selectinload(ImportBatch.course),
            selectinload(ImportBatch.category),
            selectinload(ImportBatch.images),
        )
        .where(ImportBatch.id == job_id)
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="导入任务不存在")
    return batch


def serialize_source_image(image: SourceImage) -> SourceImageResponse:
    response = SourceImageResponse.model_validate(image)
    return response.model_copy(
        update={"ocr_error": public_ocr_error_message(image.ocr_error)}
    )


def serialize_error_log(
    error_log: list[dict[str, str]],
) -> list[dict[str, str]]:
    return [
        {
            "file": item.get("file", ""),
            "message": (
                public_ocr_error_message(item.get("message"))
                or PUBLIC_OCR_ERROR_MESSAGE
            ),
        }
        for item in error_log
    ]


def serialize_job(batch: ImportBatch) -> ImportJobResponse:
    return ImportJobResponse(
        id=batch.id,
        course_name=batch.course.name,
        category_path=batch.category.path if batch.category else "",
        batch_name=batch.name,
        source_relative_path=batch.source_relative_path,
        status=batch.status,
        uploaded_count=batch.uploaded_count,
        duplicate_count=batch.duplicate_count,
        processed_count=batch.processed_count,
        found_question_count=batch.found_question_count,
        high_confidence_count=batch.high_confidence_count,
        review_count=batch.review_count,
        error_count=batch.error_count,
        current_file=batch.current_file,
        ocr_device=batch.ocr_device,
        cancel_requested=batch.cancel_requested,
        error_log=serialize_error_log(batch.error_log_json),
        created_at=batch.created_at,
        images=[serialize_source_image(image) for image in batch.images],
    )


@router.post("/preview", response_model=FolderPreviewResponse)
def preview_folder(payload: FolderPreviewRequest) -> FolderPreviewResponse:
    try:
        mappings = build_folder_mappings(payload)
    except ImportValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return FolderPreviewResponse(
        mappings=mappings,
        image_count=sum(mapping.file_count for mapping in mappings),
    )


@router.post("", response_model=ImportJobResponse, status_code=201)
def create_job(
    payload: ImportJobCreate, session: Session = Depends(get_session)
) -> ImportJobResponse:
    try:
        batch = create_import_job(session, payload)
    except ImportValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return serialize_job(get_job_or_404(session, batch.id))


@router.get("/{job_id}", response_model=ImportJobResponse)
def get_job(job_id: str, session: Session = Depends(get_session)) -> ImportJobResponse:
    return serialize_job(get_job_or_404(session, job_id))


@router.post("/{job_id}/process", response_model=ImportJobResponse, status_code=202)
def process_job(
    request: Request, job_id: str, session: Session = Depends(get_session)
) -> ImportJobResponse:
    get_job_or_404(session, job_id)
    try:
        request.app.state.ocr_worker.enqueue(job_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return serialize_job(get_job_or_404(session, job_id))


@router.post("/{job_id}/cancel", response_model=ImportJobResponse, status_code=202)
def cancel_job(
    request: Request, job_id: str, session: Session = Depends(get_session)
) -> ImportJobResponse:
    get_job_or_404(session, job_id)
    try:
        request.app.state.ocr_worker.request_cancel(job_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return serialize_job(get_job_or_404(session, job_id))


@router.post("/{job_id}/files", response_model=UploadResponse)
async def upload_files(
    request: Request,
    job_id: str,
    files: list[UploadFile] = File(...),
    relative_paths: list[str] = Form(...),
    session: Session = Depends(get_session),
) -> UploadResponse:
    if len(files) != len(relative_paths):
        raise HTTPException(status_code=400, detail="图片数量与相对路径数量不一致")
    settings: Settings = request.app.state.settings
    candidates: list[UploadCandidate] = []
    for upload, relative_path in zip(files, relative_paths, strict=True):
        content = await upload.read(settings.max_single_file_bytes + 1)
        candidates.append(
            UploadCandidate(
                original_name=upload.filename or "unnamed",
                source_relative_path=relative_path,
                content=content,
            )
        )
    batch = get_job_or_404(session, job_id)
    try:
        with request.app.state.maintenance.activity("导入图片"):
            result = import_images(session, settings, batch, candidates)
    except ImportConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ImportValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return UploadResponse(
        uploaded_count=result.uploaded_count,
        duplicate_count=result.duplicate_count,
        images=[serialize_source_image(image) for image in result.images],
    )
