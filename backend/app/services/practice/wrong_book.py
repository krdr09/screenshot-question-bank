from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Attempt, PracticeItem, WrongBookEntry
from app.models.imports import utc_now
from app.services.practice.selection import SelectionCriteria, list_practice_candidates


class WrongBookError(ValueError):
    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


def list_wrong_questions(
    db: Session,
    *,
    course_id: str | None = None,
    category_id: str | None = None,
    batch_id: str | None = None,
    include_mastered: bool = False,
) -> list[dict]:
    entries = {
        entry.question_id: entry
        for entry in db.scalars(select(WrongBookEntry)).all()
    }
    counts = dict(
        db.execute(
            select(PracticeItem.question_id, func.count(Attempt.id))
            .join(Attempt, Attempt.practice_item_id == PracticeItem.id)
            .where(
                PracticeItem.question_id.is_not(None),
                Attempt.is_correct.is_(False),
                Attempt.was_unanswered.is_(False),
            )
            .group_by(PracticeItem.question_id)
        ).all()
    )
    candidates = list_practice_candidates(
        db,
        SelectionCriteria(
            course_id=course_id,
            category_id=category_id,
            batch_id=batch_id,
        ),
    )
    result: list[dict] = []
    for selected in candidates:
        question = selected.question
        entry = entries.get(question.id)
        if entry is None or (entry.mastered_at is not None and not include_mastered):
            continue
        result.append(
            {
                "question_id": question.id,
                "question_type": question.question_type,
                "stem": question.stem,
                "options": question.options_json,
                "correct_answer": question.correct_answer_json,
                "explanation": question.explanation,
                "course_id": selected.course_id,
                "course_name": selected.course_name,
                "category_id": selected.category_id,
                "category_path": selected.category_path,
                "batch_id": selected.batch_id,
                "batch_name": selected.batch_name,
                "batch_position": selected.batch_position,
                "source_display_number": question.source_display_number,
                "display_mode": question.display_mode,
                "error_count": counts.get(question.id, 0),
                "mastered": entry.mastered_at is not None,
                "image_parts": [
                    {
                        "part_index": part.part_index,
                        "url": (
                            f"/api/questions/{question.id}/parts/{part.id}/content"
                        ),
                    }
                    for part in question.image_parts
                ],
            }
        )
    return result


def set_mastered(db: Session, question_id: str, mastered: bool) -> dict:
    entry = db.scalar(
        select(WrongBookEntry).where(WrongBookEntry.question_id == question_id)
    )
    if entry is None:
        raise WrongBookError("错题记录不存在", 404)
    entry.mastered_at = utc_now() if mastered else None
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"question_id": question_id, "mastered": mastered}
