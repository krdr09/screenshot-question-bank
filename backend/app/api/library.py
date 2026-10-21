from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.database import get_session
from app.models import CandidateImagePart, SourceImage
from app.schemas.library import (
    BatchDeleteResult,
    BatchList,
    ConfirmedLibraryAction,
    CourseList,
    DeleteResult,
    LibraryQuestion,
    LibraryQuestionPage,
    LibraryQuestionUpdate,
)
from app.services.library.batch_delete import LibraryDeletionError, delete_import_batch
from app.services.library.query import LibraryFilters, LibraryQuestion as LibraryQuestionRow, get_question, list_library_metadata, list_questions
from app.services.review.actions import rerun_candidate_ocr
from app.services.review.editor import ReviewValidationError, delete_candidate, update_candidate


router = APIRouter(tags=["题库管理"])


def _error(error: ReviewValidationError) -> HTTPException:
    status = 404 if "不存在" in str(error) else 422
    return HTTPException(status_code=status, detail=str(error))


def _question_or_404(session: Session, question_id: str) -> LibraryQuestionRow:
    item = get_question(session, question_id)
    if item is None:
        raise HTTPException(status_code=404, detail="正式题目不存在")
    return item


def _serialize(session: Session, item: LibraryQuestionRow) -> dict:
    candidate = item.candidate
    return {
        "id": candidate.id,
        "internal_uuid": candidate.id,
        "batch_id": item.batch_id,
        "batch_name": item.batch_name,
        "course_id": item.course_id,
        "course_name": item.course_name,
        "category_id": item.category_id,
        "category_path": item.category_path,
        "source_image_id": candidate.source_image_id,
        "source_image_name": candidate.source_image.original_name,
        "source_display_number": candidate.source_display_number,
        "question_type": candidate.question_type,
        "stem": candidate.stem,
        "options": candidate.options_json,
        "correct_answer": candidate.correct_answer_json,
        "true_label": candidate.true_label,
        "false_label": candidate.false_label,
        "explanation": candidate.explanation,
        "raw_ocr_text": candidate.raw_ocr_text,
        "edited_ocr_text": candidate.edited_ocr_text,
        "display_mode": candidate.display_mode,
        "status": candidate.status,
        "in_wrong_book": item.in_wrong_book,
        "parts": [
            {
                "id": part.id,
                "source_image_id": part.source_image_id,
                "part_index": part.part_index,
                "source_y_start": part.source_y_start,
                "source_y_end": part.source_y_end,
                "crop_url": f"/api/questions/{candidate.id}/parts/{part.id}/content",
                "source_url": f"/api/questions/{candidate.id}/source-images/{part.source_image_id}/content",
            }
            for part in candidate.image_parts
        ],
    }


def _safe_file(request: Request, relative_path: str) -> FileResponse:
    root = request.app.state.settings.data_dir.resolve()
    path = (root / Path(relative_path)).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(path)


@router.get("/courses", response_model=CourseList)
def courses(session: Session = Depends(get_session)) -> dict:
    course_items, _ = list_library_metadata(session)
    return {"items": course_items}


@router.get("/batches", response_model=BatchList)
def batches(
    course_id: str | None = None,
    category_id: str | None = None,
    session: Session = Depends(get_session),
) -> dict:
    _, batch_items = list_library_metadata(
        session, course_id=course_id, category_id=category_id
    )
    return {"items": batch_items}


@router.delete("/batches/{batch_id}", response_model=BatchDeleteResult)
def remove_batch(
    request: Request,
    batch_id: str,
    _: ConfirmedLibraryAction,
    session: Session = Depends(get_session),
) -> dict:
    try:
        result = delete_import_batch(session, request.app.state.settings, batch_id)
    except LibraryDeletionError as error:
        status = 404 if "不存在" in str(error) else 422
        raise HTTPException(status_code=status, detail=str(error)) from error
    return {
        "id": result.id,
        "name": result.name,
        "deleted": True,
        "deleted_questions": result.deleted_questions,
        "deleted_images": result.deleted_images,
        "cleanup_pending": result.cleanup_pending,
    }


@router.get("/questions", response_model=LibraryQuestionPage)
def questions(
    course_id: str | None = None,
    category_id: str | None = None,
    batch_id: str | None = None,
    question_type: str | None = Query(default=None, pattern="^(single_choice|true_false)$"),
    search: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> dict:
    result = list_questions(
        session,
        LibraryFilters(course_id, category_id, batch_id, question_type, search, page, page_size),
    )
    return {
        "items": [_serialize(session, item) for item in result.items],
        "total": result.total,
        "page": result.page,
        "page_size": result.page_size,
    }


@router.get("/questions/{question_id}", response_model=LibraryQuestion)
def question(question_id: str, session: Session = Depends(get_session)) -> dict:
    return _serialize(session, _question_or_404(session, question_id))


@router.patch("/questions/{question_id}", response_model=LibraryQuestion)
def edit_question(
    request: Request,
    question_id: str,
    payload: LibraryQuestionUpdate,
    session: Session = Depends(get_session),
) -> dict:
    _question_or_404(session, question_id)
    try:
        update_candidate(
            session,
            request.app.state.settings,
            question_id,
            payload.model_dump(exclude_unset=True),
        )
    except ReviewValidationError as error:
        raise _error(error) from error
    return _serialize(session, _question_or_404(session, question_id))


@router.delete("/questions/{question_id}", response_model=DeleteResult)
def remove_question(
    question_id: str,
    _: ConfirmedLibraryAction,
    session: Session = Depends(get_session),
) -> dict:
    _question_or_404(session, question_id)
    try:
        delete_candidate(session, question_id)
    except ReviewValidationError as error:
        raise _error(error) from error
    return {"id": question_id, "deleted": True}


@router.post("/questions/{question_id}/rerun-ocr", response_model=LibraryQuestion)
def rerun_question_ocr(
    request: Request,
    question_id: str,
    _: ConfirmedLibraryAction,
    session: Session = Depends(get_session),
) -> dict:
    _question_or_404(session, question_id)
    try:
        rerun_candidate_ocr(
            session,
            request.app.state.settings,
            request.app.state.ocr_worker.engine,
            question_id,
        )
    except ReviewValidationError as error:
        raise _error(error) from error
    item = get_question(session, question_id)
    if item is None:
        raise HTTPException(status_code=422, detail="重新 OCR 后题目需要人工检查")
    return _serialize(session, item)


@router.get("/questions/{question_id}/parts/{part_id}/content", response_class=FileResponse)
def question_crop(
    request: Request,
    question_id: str,
    part_id: str,
    session: Session = Depends(get_session),
) -> FileResponse:
    _question_or_404(session, question_id)
    part = session.get(CandidateImagePart, part_id)
    if part is None or part.candidate_id != question_id:
        raise HTTPException(status_code=404, detail="题目裁切图不存在")
    return _safe_file(request, part.crop_path)


@router.get("/questions/{question_id}/source-images/{image_id}/content", response_class=FileResponse)
def question_source(
    request: Request,
    question_id: str,
    image_id: str,
    session: Session = Depends(get_session),
) -> FileResponse:
    item = _question_or_404(session, question_id)
    owned_ids = {part.source_image_id for part in item.candidate.image_parts} | {
        item.candidate.source_image_id
    }
    image = session.get(SourceImage, image_id)
    if image is None or image.id not in owned_ids:
        raise HTTPException(status_code=404, detail="来源截图不存在")
    return _safe_file(request, image.relative_path)
