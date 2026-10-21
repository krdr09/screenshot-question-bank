from __future__ import annotations

from pathlib import Path
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.models import Attempt, PracticeItem, PracticeSession
from app.services.practice.image_lifecycle import practice_image_lifecycle
from app.services.practice.selection import SelectionCriteria, select_questions
from app.services.practice.snapshot_files import (
    PracticeSnapshotReferenceError,
    collect_practice_snapshot_crop_paths,
)


class PracticeSessionError(ValueError):
    pass


def list_practice_session_history(
    session: Session, page: int, page_size: int
) -> tuple[list[dict], int]:
    total = session.scalar(select(func.count()).select_from(PracticeSession)) or 0
    practices = session.scalars(
        select(PracticeSession)
        .options(
            selectinload(PracticeSession.items).selectinload(PracticeItem.attempt),
            selectinload(PracticeSession.attempts),
        )
        .order_by(PracticeSession.started_at.desc(), PracticeSession.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return [_serialize_history_item(practice) for practice in practices], total


def _serialize_history_item(practice: PracticeSession) -> dict:
    attempts = [item.attempt for item in practice.items if item.attempt is not None]
    answered_count = sum(not attempt.was_unanswered for attempt in attempts)
    submitted = practice.status == "submitted"
    return {
        "id": practice.id,
        "feedback_mode": practice.feedback_mode,
        "order_mode": practice.order_mode,
        "status": practice.status,
        "actual_count": practice.actual_count,
        "answered_count": answered_count,
        "correct_count": (
            sum(attempt.is_correct is True for attempt in attempts) if submitted else None
        ),
        "incorrect_count": (
            sum(attempt.is_correct is False and not attempt.was_unanswered for attempt in attempts)
            if submitted
            else None
        ),
        "unanswered_count": (
            practice.actual_count - answered_count if submitted else None
        ),
        "started_at": practice.started_at,
        "submitted_at": practice.submitted_at,
    }


def get_practice_session(session: Session, session_id: str) -> PracticeSession:
    practice = session.scalar(
        select(PracticeSession)
        .where(PracticeSession.id == session_id)
        .options(
            selectinload(PracticeSession.items).selectinload(PracticeItem.attempt),
            selectinload(PracticeSession.attempts),
        )
    )
    if practice is None:
        raise PracticeSessionError("刷题会话不存在")
    return practice


def create_practice_session(
    session: Session,
    criteria: SelectionCriteria,
    selection_json: dict,
    count: int | None,
    order_mode: Literal["sequential", "random"],
    feedback_mode: Literal["immediate", "deferred"],
) -> PracticeSession:
    with practice_image_lifecycle():
        return _create_practice_session(
            session,
            criteria,
            selection_json,
            count,
            order_mode,
            feedback_mode,
        )


def _create_practice_session(
    session: Session,
    criteria: SelectionCriteria,
    selection_json: dict,
    count: int | None,
    order_mode: Literal["sequential", "random"],
    feedback_mode: Literal["immediate", "deferred"],
) -> PracticeSession:
    selected, adjusted = select_questions(
        session, criteria, count, order_mode
    )
    practice = PracticeSession(
        feedback_mode=feedback_mode,
        order_mode=order_mode,
        selection_json=selection_json,
        requested_count=count,
        actual_count=len(selected),
        limit_adjusted=adjusted,
        status="active",
    )
    session.add(practice)
    session.flush()
    for order_index, selected_question in enumerate(selected):
        candidate = selected_question.question
        answer = candidate.correct_answer_json
        if not isinstance(answer, (str, bool)):
            raise PracticeSessionError("正式题目缺少可判分的正确答案")
        question_snapshot = {
            "question_id": candidate.id,
            "question_type": candidate.question_type,
            "stem": candidate.stem,
            "options": candidate.options_json,
            "display_mode": candidate.display_mode,
            "source_display_number": candidate.source_display_number,
            "course_id": selected_question.course_id,
            "course_name": selected_question.course_name,
            "category_id": selected_question.category_id,
            "category_path": selected_question.category_path,
            "batch_id": selected_question.batch_id,
            "batch_name": selected_question.batch_name,
            "batch_position": selected_question.batch_position,
        }
        image_parts = [
            {
                "part_index": part.part_index,
                "crop_path": part.crop_path,
                "source_y_start": part.source_y_start,
                "source_y_end": part.source_y_end,
            }
            for part in sorted(candidate.image_parts, key=lambda value: value.part_index)
        ]
        session.add(
            PracticeItem(
                session=practice,
                question_id=candidate.id,
                order_index=order_index,
                question_snapshot_json=question_snapshot,
                answer_snapshot_json=answer,
                explanation_snapshot=candidate.explanation,
                image_parts_snapshot_json=image_parts,
            )
        )
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    return get_practice_session(session, practice.id)


def feedback_allowed(practice: PracticeSession, attempt: Attempt | None) -> bool:
    if practice.status == "submitted":
        return True
    return (
        practice.feedback_mode == "immediate"
        and attempt is not None
        and attempt.locked_at is not None
    )


def _serialize_item(practice: PracticeSession, item: PracticeItem) -> dict:
    snapshot = item.question_snapshot_json
    attempt = item.attempt
    result = {
        "id": item.id,
        "question_id": snapshot["question_id"],
        "order_index": item.order_index,
        "question_type": snapshot["question_type"],
        "stem": snapshot["stem"],
        "options": snapshot["options"],
        "display_mode": snapshot["display_mode"],
        "source_display_number": snapshot["source_display_number"],
        "course_id": snapshot["course_id"],
        "course_name": snapshot["course_name"],
        "category_id": snapshot["category_id"],
        "category_path": snapshot["category_path"],
        "batch_id": snapshot["batch_id"],
        "batch_name": snapshot["batch_name"],
        "batch_position": snapshot["batch_position"],
        "image_parts": [
            {
                "part_index": part["part_index"],
                "source_y_start": part["source_y_start"],
                "source_y_end": part["source_y_end"],
                "url": (
                    f"/api/practice-sessions/{practice.id}/items/{item.id}"
                    f"/parts/{part['part_index']}/content"
                ),
            }
            for part in item.image_parts_snapshot_json
        ],
        "selected_answer": attempt.selected_answer_json if attempt else None,
        "saved": attempt is not None and not attempt.was_unanswered,
        "locked": attempt is not None and attempt.locked_at is not None,
        "was_unanswered": attempt.was_unanswered if attempt else False,
    }
    if feedback_allowed(practice, attempt):
        result.update(
            {
                "is_correct": attempt.is_correct if attempt else None,
                "correct_answer": item.answer_snapshot_json,
                "explanation": item.explanation_snapshot,
            }
        )
    return result


def serialize_session(practice: PracticeSession) -> dict:
    notice = None
    if practice.limit_adjusted and practice.requested_count is not None:
        notice = (
            f"请求 {practice.requested_count} 题，当前可用 {practice.actual_count} 题，"
            f"已使用全部 {practice.actual_count} 题。"
        )
    return {
        "id": practice.id,
        "feedback_mode": practice.feedback_mode,
        "order_mode": practice.order_mode,
        "status": practice.status,
        "requested_count": practice.requested_count,
        "actual_count": practice.actual_count,
        "limit_adjusted": practice.limit_adjusted,
        "notice": notice,
        "started_at": practice.started_at,
        "submitted_at": practice.submitted_at,
        "answered_count": sum(
            item.attempt is not None and not item.attempt.was_unanswered
            for item in practice.items
        ),
        "items": [_serialize_item(practice, item) for item in practice.items],
    }


def get_snapshot_part_path(
    session: Session,
    settings: Settings,
    session_id: str,
    item_id: str,
    part_index: int,
) -> Path:
    item = session.scalar(
        select(PracticeItem).where(
            PracticeItem.id == item_id,
            PracticeItem.session_id == session_id,
        )
    )
    if item is None:
        raise PracticeSessionError("刷题会话中的题目不存在")
    try:
        safe_crop_paths = collect_practice_snapshot_crop_paths(
            [item.image_parts_snapshot_json]
        )
    except PracticeSnapshotReferenceError as error:
        raise PracticeSessionError("题目裁切图片不存在") from error
    part = next(
        (
            value
            for value in item.image_parts_snapshot_json
            if value.get("part_index") == part_index
        ),
        None,
    )
    if part is None:
        raise PracticeSessionError("题目裁切片段不存在")
    crop_path = part["crop_path"]
    if crop_path not in safe_crop_paths:
        raise PracticeSessionError("题目裁切图片不存在")
    crop_root = (settings.data_dir / "crops").resolve()
    path = (crop_root / Path(crop_path).relative_to("crops")).resolve()
    if crop_root not in path.parents or not path.is_file():
        raise PracticeSessionError("题目裁切图片不存在")
    return path
