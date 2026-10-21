from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.database import get_session
from app.schemas.practice import (
    PracticeAnswerRequest,
    PracticeCandidatePage,
    PracticeItemResponse,
    PracticeResult,
    PracticeSessionCreate,
    PracticeSessionHistoryPage,
    PracticeSessionResponse,
)
from app.services.practice.grading import (
    PracticeGradingError,
    get_result,
    save_answer,
    submit_session,
)
from app.services.practice.sessions import (
    PracticeSessionError,
    create_practice_session,
    get_practice_session,
    get_snapshot_part_path,
    list_practice_session_history,
    serialize_session,
)
from app.services.practice.selection import (
    PracticeSelectionError,
    SelectionCriteria,
    list_practice_candidates,
)


router = APIRouter(tags=["刷题"])


def _criteria(payload) -> SelectionCriteria:
    return SelectionCriteria(
        course_id=payload.course_id,
        category_id=payload.category_id,
        batch_id=payload.batch_id,
        question_type=payload.question_type,
        membership=payload.membership,
        manual_question_ids=tuple(payload.manual_question_ids),
        position_start=payload.position_start,
        position_end=payload.position_end,
    )


def _session_error(error: PracticeSessionError) -> HTTPException:
    code = 404 if "不存在" in str(error) else 422
    return HTTPException(status_code=code, detail=str(error))


@router.get("/practice-candidates", response_model=PracticeCandidatePage)
def practice_candidates(
    course_id: str | None = None,
    category_id: str | None = None,
    batch_id: str | None = None,
    question_type: Literal["single_choice", "true_false"] | None = None,
    membership: Literal["all", "wrong", "unanswered"] = "all",
    position_start: int | None = Query(default=None, ge=1),
    position_end: int | None = Query(default=None, ge=1),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> dict:
    try:
        items = list_practice_candidates(
            session,
            SelectionCriteria(
                course_id=course_id,
                category_id=category_id,
                batch_id=batch_id,
                question_type=question_type,
                membership=membership,
                position_start=position_start,
                position_end=position_end,
            ),
        )
    except PracticeSelectionError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    start = (page - 1) * page_size
    page_items = items[start : start + page_size]
    return {
        "items": [
            {
                "id": item.question.id,
                "question_type": item.question.question_type,
                "stem": item.question.stem,
                "course_id": item.course_id,
                "course_name": item.course_name,
                "category_id": item.category_id,
                "category_path": item.category_path,
                "batch_id": item.batch_id,
                "batch_name": item.batch_name,
                "batch_position": item.batch_position,
                "source_display_number": item.question.source_display_number,
                "in_wrong_book": item.in_wrong_book,
                "has_answered": item.has_answered,
            }
            for item in page_items
        ],
        "total": len(items),
        "page": page,
        "page_size": page_size,
    }


@router.post(
    "/practice-sessions",
    response_model=PracticeSessionResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
)
def create_session(
    payload: PracticeSessionCreate,
    session: Session = Depends(get_session),
) -> dict:
    try:
        practice = create_practice_session(
            session,
            _criteria(payload.selection),
            payload.selection.model_dump(),
            payload.count,
            payload.order_mode,
            payload.feedback_mode,
        )
    except PracticeSelectionError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except PracticeSessionError as error:
        raise _session_error(error) from error
    return serialize_session(practice)


@router.get("/practice-sessions", response_model=PracticeSessionHistoryPage)
def practice_history(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    items, total = list_practice_session_history(session, page, page_size)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get(
    "/practice-sessions/{session_id}",
    response_model=PracticeSessionResponse,
    response_model_exclude_none=True,
)
def practice_session(
    session_id: str,
    item_id: str | None = None,
    order_index: int | None = Query(default=None, ge=0),
    session: Session = Depends(get_session),
) -> dict:
    if item_id is not None and order_index is not None:
        raise HTTPException(status_code=422, detail="item_id 和 order_index 只能使用一个")
    try:
        payload = serialize_session(get_practice_session(session, session_id))
    except PracticeSessionError as error:
        raise _session_error(error) from error
    if item_id is not None:
        payload["items"] = [item for item in payload["items"] if item["id"] == item_id]
    elif order_index is not None:
        payload["items"] = [
            item for item in payload["items"] if item["order_index"] == order_index
        ]
    if (item_id is not None or order_index is not None) and not payload["items"]:
        raise HTTPException(status_code=404, detail="刷题会话中的题目不存在")
    return payload


@router.get(
    "/practice-sessions/{session_id}/items/{item_id}/parts/{part_index}/content",
    response_class=FileResponse,
)
def practice_crop(
    request: Request,
    session_id: str,
    item_id: str,
    part_index: int,
    session: Session = Depends(get_session),
) -> FileResponse:
    try:
        path = get_snapshot_part_path(
            session,
            request.app.state.settings,
            session_id,
            item_id,
            part_index,
        )
    except PracticeSessionError as error:
        raise _session_error(error) from error
    return FileResponse(path)


def _grading_error(error: PracticeGradingError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=str(error))


@router.post(
    "/practice-sessions/{session_id}/answers",
    response_model=PracticeItemResponse,
    response_model_exclude_none=True,
)
def answer(
    session_id: str,
    payload: PracticeAnswerRequest,
    session: Session = Depends(get_session),
) -> dict:
    try:
        return save_answer(
            session, session_id, payload.practice_item_id, payload.selected_answer
        )
    except PracticeGradingError as error:
        raise _grading_error(error) from error
    except PracticeSessionError as error:
        raise _session_error(error) from error


@router.post(
    "/practice-sessions/{session_id}/submit",
    response_model=PracticeResult,
    response_model_exclude_none=True,
)
def submit(session_id: str, session: Session = Depends(get_session)) -> dict:
    try:
        return submit_session(session, session_id)
    except PracticeGradingError as error:
        raise _grading_error(error) from error
    except PracticeSessionError as error:
        raise _session_error(error) from error


@router.get(
    "/practice-sessions/{session_id}/result",
    response_model=PracticeResult,
    response_model_exclude_none=True,
)
def result(session_id: str, session: Session = Depends(get_session)) -> dict:
    try:
        return get_result(session, session_id)
    except PracticeGradingError as error:
        raise _grading_error(error) from error
    except PracticeSessionError as error:
        raise _session_error(error) from error
