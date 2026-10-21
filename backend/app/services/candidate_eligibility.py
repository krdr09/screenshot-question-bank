from __future__ import annotations

from sqlalchemy import and_, exists, select
from sqlalchemy.sql.elements import ColumnElement

from app.models import CandidateImagePart, QuestionCandidate, SourceImage


def ready_candidate_predicate() -> ColumnElement[bool]:
    excluded_primary = exists(
        select(SourceImage.id).where(
            SourceImage.id == QuestionCandidate.source_image_id,
            SourceImage.review_flag.is_not(None),
        )
    )
    excluded_part = exists(
        select(CandidateImagePart.id)
        .join(SourceImage, SourceImage.id == CandidateImagePart.source_image_id)
        .where(
            CandidateImagePart.candidate_id == QuestionCandidate.id,
            SourceImage.review_flag.is_not(None),
        )
    )
    return and_(
        QuestionCandidate.status == "ready",
        ~excluded_primary,
        ~excluded_part,
    )
