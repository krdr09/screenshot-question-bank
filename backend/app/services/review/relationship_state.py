from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import CandidateRelationship
from app.services.review.editor import ReviewValidationError


PROCESSING_RELATIONSHIP_STATUS = "_processing"


class ReviewRelationshipConflictError(ReviewValidationError):
    pass


def claim_pending_relationship(
    session: Session,
    relationship_id: str,
) -> CandidateRelationship:
    result = session.execute(
        update(CandidateRelationship)
        .where(
            CandidateRelationship.id == relationship_id,
            CandidateRelationship.status == "pending",
        )
        .values(status=PROCESSING_RELATIONSHIP_STATUS)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 1:
        relationship = session.scalar(
            select(CandidateRelationship)
            .where(CandidateRelationship.id == relationship_id)
            .execution_options(populate_existing=True)
        )
        if relationship is None:
            raise ReviewValidationError("候选关系不存在")
        return relationship

    relationship = session.scalar(
        select(CandidateRelationship)
        .where(CandidateRelationship.id == relationship_id)
        .execution_options(populate_existing=True)
    )
    if relationship is None:
        raise ReviewValidationError("候选关系不存在")
    raise ReviewRelationshipConflictError("候选关系已处理，请刷新后重试")
