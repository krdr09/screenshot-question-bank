from __future__ import annotations

from collections import defaultdict

from sqlalchemy import case, exists, or_, select, union
from sqlalchemy.orm import Session

from app.models import (
    CandidateImagePart,
    CandidateRelationship,
    ImportBatch,
    QuestionCandidate,
    SourceImage,
)
from app.services.review.editor import (
    INACTIVE_CANDIDATE_STATUSES,
    update_batch_review_state,
)


EXCLUDED_SOURCE_FLAGS = frozenset({"duplicate", "unrelated", "damaged"})
EXCLUDED_SOURCE_ANOMALY = {
    "code": "EXCLUDED_SOURCE_IMAGE",
    "severity": "warning",
    "message": "候选题包含已在顺序检查中排除的来源图片，请检查并调整图片片段",
}


def _append_excluded_source_anomaly(candidate: QuestionCandidate) -> None:
    flags = list(candidate.anomaly_flags_json or [])
    if not any(flag.get("code") == EXCLUDED_SOURCE_ANOMALY["code"] for flag in flags):
        flags.append(dict(EXCLUDED_SOURCE_ANOMALY))
        candidate.anomaly_flags_json = flags


def _resolve_excluded_relationships(
    relationships: list[CandidateRelationship],
    fully_excluded_ids: set[str],
) -> None:
    for relationship in relationships:
        primary_excluded = relationship.primary_candidate_id in fully_excluded_ids
        secondary_excluded = relationship.secondary_candidate_id in fully_excluded_ids
        if not primary_excluded and not secondary_excluded:
            continue
        relationship.status = "resolved"
        if primary_excluded and secondary_excluded:
            relationship.decision = "source_images_excluded"
        elif primary_excluded:
            relationship.decision = "kept_secondary"
        else:
            relationship.decision = "kept_primary"


def _coordination_scope(batch_id: str):
    primary_from_batch = exists(
        select(SourceImage.id).where(
            SourceImage.id == QuestionCandidate.source_image_id,
            SourceImage.batch_id == batch_id,
        )
    )
    part_from_batch = exists(
        select(CandidateImagePart.id)
        .join(SourceImage, SourceImage.id == CandidateImagePart.source_image_id)
        .where(
            CandidateImagePart.candidate_id == QuestionCandidate.id,
            SourceImage.batch_id == batch_id,
        )
    )
    coordinated_candidates = (
        select(QuestionCandidate.id.label("id"))
        .where(
            or_(
                QuestionCandidate.batch_id == batch_id,
                primary_from_batch,
                part_from_batch,
            )
        )
        .cte("coordinated_candidate_ids", recursive=True)
    )
    connected_candidate = (
        select(
            case(
                (
                    CandidateRelationship.primary_candidate_id
                    == coordinated_candidates.c.id,
                    CandidateRelationship.secondary_candidate_id,
                ),
                else_=CandidateRelationship.primary_candidate_id,
            ).label("id")
        )
        .select_from(CandidateRelationship)
        .join(
            coordinated_candidates,
            or_(
                CandidateRelationship.primary_candidate_id
                == coordinated_candidates.c.id,
                CandidateRelationship.secondary_candidate_id
                == coordinated_candidates.c.id,
            ),
        )
        .where(CandidateRelationship.status == "pending")
    )
    coordinated_candidates = coordinated_candidates.union(connected_candidate)
    pending_relationships = (
        select(
            CandidateRelationship.id,
            CandidateRelationship.primary_candidate_id,
            CandidateRelationship.secondary_candidate_id,
        )
        .where(
            CandidateRelationship.status == "pending",
            exists(
                select(coordinated_candidates.c.id).where(
                    or_(
                        coordinated_candidates.c.id
                        == CandidateRelationship.primary_candidate_id,
                        coordinated_candidates.c.id
                        == CandidateRelationship.secondary_candidate_id,
                    )
                )
            ),
        )
        .cte("coordinated_pending_relationships")
    )
    return coordinated_candidates, pending_relationships


def reconcile_excluded_sources(session: Session, batch_id: str) -> ImportBatch:
    session.flush()
    coordinated_ids, relationship_ids = _coordination_scope(batch_id)
    candidates = list(
        session.scalars(
            select(QuestionCandidate)
            .join(
                coordinated_ids,
                coordinated_ids.c.id == QuestionCandidate.id,
            )
        )
    )
    primary_sources = (
        select(
            QuestionCandidate.id.label("candidate_id"),
            QuestionCandidate.source_image_id.label("source_image_id"),
        )
        .join(coordinated_ids, coordinated_ids.c.id == QuestionCandidate.id)
    )
    part_sources = (
        select(
            CandidateImagePart.candidate_id.label("candidate_id"),
            CandidateImagePart.source_image_id.label("source_image_id"),
        )
        .join(
            coordinated_ids,
            coordinated_ids.c.id == CandidateImagePart.candidate_id,
        )
    )
    candidate_sources = union(primary_sources, part_sources).cte("candidate_sources")
    source_flags_by_candidate: defaultdict[str, dict[str, str | None]] = defaultdict(
        dict
    )
    for candidate_id, source_image_id, review_flag in session.execute(
        select(
            candidate_sources.c.candidate_id,
            candidate_sources.c.source_image_id,
            SourceImage.review_flag,
        ).join(SourceImage, SourceImage.id == candidate_sources.c.source_image_id)
    ):
        source_flags_by_candidate[candidate_id][source_image_id] = review_flag

    fully_excluded_ids: set[str] = set()
    for candidate in candidates:
        source_flags = source_flags_by_candidate[candidate.id]
        source_ids = set(source_flags)
        excluded_ids = {
            image_id
            for image_id in source_ids
            if source_flags.get(image_id) in EXCLUDED_SOURCE_FLAGS
        }
        if excluded_ids == source_ids and source_ids:
            if candidate.status not in INACTIVE_CANDIDATE_STATUSES:
                candidate.status = "ignored"
            if candidate.status == "ignored":
                fully_excluded_ids.add(candidate.id)
        elif excluded_ids and candidate.status not in INACTIVE_CANDIDATE_STATUSES:
            candidate.status = "review"
            _append_excluded_source_anomaly(candidate)

    relationships = list(
        session.scalars(
            select(CandidateRelationship).join(
                relationship_ids,
                relationship_ids.c.id == CandidateRelationship.id,
            )
        )
    )
    _resolve_excluded_relationships(relationships, fully_excluded_ids)
    session.flush()
    affected_batch_ids = {batch_id}
    affected_batch_ids.update(candidate.batch_id for candidate in candidates)
    affected_batch_ids.update(relationship.batch_id for relationship in relationships)

    requested_batch: ImportBatch | None = None
    for affected_batch_id in sorted(affected_batch_ids):
        reconciled_batch = update_batch_review_state(session, affected_batch_id)
        if affected_batch_id == batch_id:
            requested_batch = reconciled_batch
    assert requested_batch is not None
    return requested_batch
