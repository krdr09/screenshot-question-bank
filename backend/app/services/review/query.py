from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Select, and_, exists, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Category,
    Course,
    ImportBatch,
    QuestionCandidate,
    ReviewAction,
    SourceImage,
)
from app.services.review.editor import INACTIVE_CANDIDATE_STATUSES


@dataclass(frozen=True)
class ReviewFilters:
    batch_id: str | None = None
    course_id: str | None = None
    category_id: str | None = None
    course_name: str | None = None
    category_path: str | None = None
    batch_name: str | None = None
    anomaly_code: str | None = None
    question_type: str | None = None
    severity: str | None = None
    handling_status: str = "pending"


@dataclass(frozen=True)
class ReviewItem:
    candidate: QuestionCandidate
    course_name: str
    category_path: str
    batch_name: str


def _review_candidate_statement() -> Select[tuple[QuestionCandidate]]:
    return (
        select(QuestionCandidate)
        .join(SourceImage, SourceImage.id == QuestionCandidate.source_image_id)
        .join(ImportBatch, ImportBatch.id == QuestionCandidate.batch_id)
        .join(Course, Course.id == ImportBatch.course_id)
        .outerjoin(Category, Category.id == ImportBatch.category_id)
        .options(
            selectinload(QuestionCandidate.source_image)
            .selectinload(SourceImage.batch)
            .selectinload(ImportBatch.course),
            selectinload(QuestionCandidate.source_image)
            .selectinload(SourceImage.batch)
            .selectinload(ImportBatch.category),
            selectinload(QuestionCandidate.image_parts),
        )
        .order_by(
            SourceImage.order_index,
            QuestionCandidate.created_at,
            QuestionCandidate.id,
        )
    )


def _review_item(candidate: QuestionCandidate) -> ReviewItem:
    batch = candidate.source_image.batch
    return ReviewItem(
        candidate=candidate,
        course_name=batch.course.name,
        category_path=batch.category.path if batch.category else "",
        batch_name=batch.name,
    )


def list_review_items(session: Session, filters: ReviewFilters) -> list[ReviewItem]:
    statement = _review_candidate_statement()
    if filters.batch_id:
        statement = statement.where(QuestionCandidate.batch_id == filters.batch_id)
    if filters.course_id:
        statement = statement.where(ImportBatch.course_id == filters.course_id)
    if filters.category_id:
        statement = statement.where(ImportBatch.category_id == filters.category_id)
    if filters.course_name:
        statement = statement.where(Course.name == filters.course_name)
    if filters.category_path:
        statement = statement.where(Category.path == filters.category_path)
    if filters.batch_name:
        statement = statement.where(ImportBatch.name == filters.batch_name)
    if filters.question_type:
        statement = statement.where(QuestionCandidate.question_type == filters.question_type)
    has_active_review_action = exists().where(
        ReviewAction.candidate_id == QuestionCandidate.id,
        ReviewAction.undone_at.is_(None),
    )
    pending = QuestionCandidate.status == "review"
    processed = and_(
        QuestionCandidate.status != "review",
        has_active_review_action,
    )
    if filters.handling_status == "pending":
        statement = statement.where(pending)
    elif filters.handling_status == "processed":
        statement = statement.where(processed)
    elif filters.handling_status == "all":
        statement = statement.where(or_(pending, processed))

    result: list[ReviewItem] = []
    for candidate in session.scalars(statement).unique():
        flags = candidate.anomaly_flags_json or []
        if filters.anomaly_code and not any(
            flag.get("code") == filters.anomaly_code for flag in flags
        ):
            continue
        if filters.severity and not any(
            flag.get("severity") == filters.severity for flag in flags
        ):
            continue
        result.append(_review_item(candidate))
    return result


def get_review_item(session: Session, candidate_id: str) -> ReviewItem | None:
    candidate = (
        session.scalars(
            _review_candidate_statement().where(
                QuestionCandidate.id == candidate_id
            )
        )
        .unique()
        .one_or_none()
    )
    return _review_item(candidate) if candidate is not None else None


def list_batch_context_items(
    session: Session,
    candidate_id: str,
) -> list[QuestionCandidate] | None:
    anchor = session.get(QuestionCandidate, candidate_id)
    if anchor is None:
        return None
    statement = (
        select(QuestionCandidate)
        .join(SourceImage, SourceImage.id == QuestionCandidate.source_image_id)
        .where(
            QuestionCandidate.batch_id == anchor.batch_id,
            QuestionCandidate.status.not_in(INACTIVE_CANDIDATE_STATUSES),
        )
        .order_by(
            SourceImage.order_index,
            QuestionCandidate.created_at,
            QuestionCandidate.id,
        )
    )
    return list(session.scalars(statement))
