from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_session
from app.schemas.wrong_book import WrongBookMutationResponse, WrongQuestionPage
from app.services.practice.wrong_book import (
    WrongBookError,
    list_wrong_questions,
    set_mastered,
)


router = APIRouter(tags=["错题本"])


@router.get("/wrong-questions", response_model=WrongQuestionPage)
def wrong_questions(
    course_id: str | None = None,
    category_id: str | None = None,
    batch_id: str | None = None,
    include_mastered: bool = False,
    session: Session = Depends(get_session),
) -> dict:
    items = list_wrong_questions(
        session,
        course_id=course_id,
        category_id=category_id,
        batch_id=batch_id,
        include_mastered=include_mastered,
    )
    return {"items": items, "total": len(items)}


def _set(question_id: str, mastered: bool, session: Session) -> dict:
    try:
        return set_mastered(session, question_id, mastered)
    except WrongBookError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error


@router.post(
    "/wrong-questions/{question_id}/master",
    response_model=WrongBookMutationResponse,
)
def master(question_id: str, session: Session = Depends(get_session)) -> dict:
    return _set(question_id, True, session)


@router.post(
    "/wrong-questions/{question_id}/restore",
    response_model=WrongBookMutationResponse,
)
def restore(question_id: str, session: Session = Depends(get_session)) -> dict:
    return _set(question_id, False, session)
