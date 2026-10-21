from __future__ import annotations

from sqlalchemy import literal, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Attempt, PracticeItem, PracticeSession, QuestionCandidate, WrongBookEntry
from app.models.imports import new_uuid, utc_now
from app.services.practice.sessions import get_practice_session, serialize_session


class PracticeGradingError(ValueError):
    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


def normalize_selected_answer(item: PracticeItem, value: bool | str) -> bool | str:
    snapshot = item.question_snapshot_json
    question_type = snapshot.get("question_type")
    if question_type == "true_false":
        if type(value) is not bool:
            raise ValueError("判断题答案必须是布尔值")
        return value
    if question_type != "single_choice":
        raise ValueError("不支持的题型")
    if type(value) is not str:
        raise ValueError("单选题答案必须是选项标签")
    normalized = value.strip().upper()
    labels = {
        str(option.get("label", "")).strip().upper()
        for option in snapshot.get("options", [])
    }
    if normalized not in labels:
        raise ValueError("单选题答案不在可选项中")
    return normalized


def _item_or_error(
    db: Session, practice: PracticeSession, item_id: str
) -> PracticeItem:
    item = db.scalar(
        select(PracticeItem).where(
            PracticeItem.id == item_id,
            PracticeItem.session_id == practice.id,
        )
    )
    if item is None:
        raise PracticeGradingError("刷题会话中的题目不存在", 404)
    return item


def _record_wrong(db: Session, question_id: str | None) -> None:
    if question_id is None:
        return
    now = utc_now()
    eligible_candidate = select(
        literal(new_uuid()),
        QuestionCandidate.id,
        literal(now),
        literal(None),
        literal(now),
    ).where(
        QuestionCandidate.id == question_id,
        QuestionCandidate.status == "ready",
    )
    statement = sqlite_insert(WrongBookEntry).from_select(
        ["id", "question_id", "created_at", "mastered_at", "updated_at"],
        eligible_candidate,
    ).on_conflict_do_update(
        index_elements=[WrongBookEntry.question_id],
        set_={"mastered_at": None, "updated_at": now},
    )
    db.execute(statement)


def save_answer(
    db: Session,
    session_id: str,
    item_id: str,
    selected_answer: bool | str,
) -> dict:
    return _save_answer(db, session_id, item_id, selected_answer, retry_conflict=True)


def _save_answer(
    db: Session,
    session_id: str,
    item_id: str,
    selected_answer: bool | str,
    *,
    retry_conflict: bool,
) -> dict:
    practice = get_practice_session(db, session_id)
    if practice.status == "submitted":
        raise PracticeGradingError("已提交的刷题会话不能修改答案", 409)
    item = _item_or_error(db, practice, item_id)
    try:
        normalized = normalize_selected_answer(item, selected_answer)
    except ValueError as error:
        raise PracticeGradingError(str(error), 422) from error

    attempt = item.attempt
    if practice.feedback_mode == "immediate" and attempt and attempt.locked_at:
        if attempt.selected_answer_json != normalized:
            raise PracticeGradingError("即时反馈模式下答案已锁定", 409)
        return next(
            value
            for value in serialize_session(practice)["items"]
            if value["id"] == item.id
        )

    now = utc_now()
    if attempt is None:
        attempt = Attempt(
            session=practice,
            item=item,
            selected_answer_json=normalized,
            answered_at=now,
            was_unanswered=False,
        )
        db.add(attempt)
    else:
        attempt.selected_answer_json = normalized
        attempt.was_unanswered = False
        attempt.answered_at = attempt.answered_at or now

    try:
        if practice.feedback_mode == "immediate":
            attempt.is_correct = normalized == item.answer_snapshot_json
            attempt.locked_at = now
            if attempt.is_correct is False:
                _record_wrong(db, item.question_id)
        else:
            attempt.is_correct = None
            attempt.locked_at = None
        db.commit()
    except IntegrityError:
        db.rollback()
        if not retry_conflict:
            raise
        return _save_answer(
            db,
            session_id,
            item_id,
            selected_answer,
            retry_conflict=False,
        )
    except Exception:
        db.rollback()
        raise
    practice = get_practice_session(db, session_id)
    return next(
        value
        for value in serialize_session(practice)["items"]
        if value["id"] == item_id
    )


def _result(practice: PracticeSession) -> dict:
    serialized = serialize_session(practice)
    attempts = [item.attempt for item in practice.items]
    correct = sum(attempt is not None and attempt.is_correct is True for attempt in attempts)
    unanswered = sum(
        attempt is None or attempt.was_unanswered for attempt in attempts
    )
    incorrect = sum(
        attempt is not None
        and not attempt.was_unanswered
        and attempt.is_correct is False
        for attempt in attempts
    )
    total = len(practice.items)
    return {
        "session_id": practice.id,
        "total_count": total,
        "correct_count": correct,
        "incorrect_count": incorrect,
        "unanswered_count": unanswered,
        "accuracy": round(correct * 100 / total, 2) if total else 0.0,
        "items": serialized["items"],
    }


def submit_session(db: Session, session_id: str) -> dict:
    return _submit_session(db, session_id, retry_conflict=True)


def _submit_session(
    db: Session, session_id: str, *, retry_conflict: bool
) -> dict:
    practice = get_practice_session(db, session_id)
    if practice.status == "submitted":
        return _result(practice)

    try:
        now = utc_now()
        for item in practice.items:
            attempt = item.attempt
            if attempt is None:
                attempt = Attempt(
                    session=practice,
                    item=item,
                    selected_answer_json=None,
                    is_correct=None,
                    was_unanswered=True,
                    locked_at=now,
                )
                db.add(attempt)
                continue
            if practice.feedback_mode == "deferred":
                attempt.is_correct = (
                    attempt.selected_answer_json == item.answer_snapshot_json
                )
                attempt.locked_at = now
                if attempt.is_correct is False:
                    _record_wrong(db, item.question_id)

        practice.status = "submitted"
        practice.submitted_at = now
        db.commit()
    except IntegrityError:
        db.rollback()
        if not retry_conflict:
            raise
        return _submit_session(db, session_id, retry_conflict=False)
    except Exception:
        db.rollback()
        raise
    return _result(get_practice_session(db, session_id))


def get_result(db: Session, session_id: str) -> dict:
    practice = get_practice_session(db, session_id)
    if practice.status != "submitted":
        raise PracticeGradingError("刷题会话尚未提交", 409)
    return _result(practice)
