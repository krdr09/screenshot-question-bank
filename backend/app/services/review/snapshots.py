from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CandidateImagePart, QuestionCandidate, ReviewAction


CANDIDATE_FIELDS = (
    "category_id",
    "source_display_number",
    "question_type",
    "stem",
    "options_json",
    "correct_answer_json",
    "source_correct_answer_raw",
    "true_label",
    "false_label",
    "explanation",
    "raw_ocr_text",
    "edited_ocr_text",
    "normalized_ocr_text",
    "ocr_confidence",
    "parse_confidence",
    "display_mode",
    "anomaly_flags_json",
    "status",
    "duplicate_of_id",
    "content_fingerprint",
)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _next_action_created_at(session: Session, candidate_id: str) -> datetime:
    session.flush()
    latest = session.scalar(
        select(func.max(ReviewAction.created_at)).where(
            ReviewAction.candidate_id == candidate_id
        )
    )
    now = datetime.now(timezone.utc)
    if latest is None:
        return now
    latest_utc = _as_utc(latest)
    if now <= latest_utc:
        return latest_utc + timedelta(microseconds=1)
    return now


def capture_candidate(candidate: QuestionCandidate) -> dict:
    return {
        "candidate": {field: getattr(candidate, field) for field in CANDIDATE_FIELDS},
        "parts": [
            {
                "id": part.id,
                "source_image_id": part.source_image_id,
                "crop_path": part.crop_path,
                "source_y_start": part.source_y_start,
                "source_y_end": part.source_y_end,
                "part_index": part.part_index,
            }
            for part in sorted(candidate.image_parts, key=lambda item: item.part_index)
        ],
    }


def restore_candidate(session: Session, candidate: QuestionCandidate, snapshot: dict) -> None:
    for field, value in snapshot["candidate"].items():
        setattr(candidate, field, value)
    for part in list(candidate.image_parts):
        session.delete(part)
    session.flush()
    candidate.image_parts = [
        CandidateImagePart(candidate_id=candidate.id, **part) for part in snapshot["parts"]
    ]
    session.flush()


def record_action(
    session: Session,
    candidate: QuestionCandidate,
    action_type: str,
    before: dict,
    after: dict,
    *,
    target_type: str = "candidate",
    target_id: str | None = None,
) -> ReviewAction:
    action = ReviewAction(
        batch_id=candidate.batch_id,
        candidate_id=candidate.id,
        target_type=target_type,
        target_id=target_id or candidate.id,
        action_type=action_type,
        before_json=before,
        after_json=after,
        created_at=_next_action_created_at(session, candidate.id),
    )
    session.add(action)
    session.flush()
    return action


def mark_undone(action: ReviewAction) -> None:
    action.undone_at = datetime.now(timezone.utc)
