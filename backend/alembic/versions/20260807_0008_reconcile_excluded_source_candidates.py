"""收敛被顺序检查排除的来源图片所生成的候选题。"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260807_0008"
down_revision: str | None = "20260714_0007"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

EXCLUDED_FLAGS = {"duplicate", "unrelated", "damaged"}
INACTIVE_STATUSES = {
    "deleted",
    "ignored",
    "duplicate_merged",
    "merged_source",
    "replaced_by_ocr",
}
ANOMALY = {
    "code": "EXCLUDED_SOURCE_IMAGE",
    "severity": "warning",
    "message": "候选题包含已在顺序检查中排除的来源图片，请检查并调整图片片段",
}


def _flags(value) -> list[dict]:
    if isinstance(value, list):
        return list(value)
    return list(json.loads(value or "[]"))


def upgrade() -> None:
    connection = op.get_bind()
    metadata = sa.MetaData()
    source_images = sa.Table("source_images", metadata, autoload_with=connection)
    candidates = sa.Table("question_candidates", metadata, autoload_with=connection)
    parts = sa.Table("candidate_image_parts", metadata, autoload_with=connection)
    relationships = sa.Table(
        "candidate_relationships", metadata, autoload_with=connection
    )
    ordering_issues = sa.Table("ordering_issues", metadata, autoload_with=connection)
    batches = sa.Table("import_batches", metadata, autoload_with=connection)

    flagged_images = (
        sa.select(
            source_images.c.id.label("image_id"),
            source_images.c.batch_id.label("batch_id"),
        )
        .where(source_images.c.review_flag.is_not(None))
        .cte("flagged_images")
    )
    affected_batches = sa.union(
        sa.select(flagged_images.c.batch_id),
        sa.select(candidates.c.batch_id).join(
            flagged_images,
            flagged_images.c.image_id == candidates.c.source_image_id,
        ),
        sa.select(candidates.c.batch_id)
        .join(parts, parts.c.candidate_id == candidates.c.id)
        .join(
            flagged_images,
            flagged_images.c.image_id == parts.c.source_image_id,
        ),
    ).cte("affected_batch_ids")
    affected_batch_ids = set(
        connection.scalars(sa.select(affected_batches.c.batch_id))
    )
    if not affected_batch_ids:
        return

    candidate_rows = list(
        connection.execute(
            sa.select(
                candidates.c.id,
                candidates.c.status,
                candidates.c.anomaly_flags_json,
            ).join(
                affected_batches,
                affected_batches.c.batch_id == candidates.c.batch_id,
            )
        ).mappings()
    )
    candidate_statuses = {row["id"]: row["status"] for row in candidate_rows}
    coordinated_candidate_ids = set(candidate_statuses)

    primary_sources = (
        sa.select(
            candidates.c.id.label("candidate_id"),
            candidates.c.source_image_id.label("source_image_id"),
        )
        .join(
            affected_batches,
            affected_batches.c.batch_id == candidates.c.batch_id,
        )
    )
    part_sources = (
        sa.select(
            parts.c.candidate_id.label("candidate_id"),
            parts.c.source_image_id.label("source_image_id"),
        )
        .join(candidates, candidates.c.id == parts.c.candidate_id)
        .join(
            affected_batches,
            affected_batches.c.batch_id == candidates.c.batch_id,
        )
    )
    candidate_sources = sa.union(primary_sources, part_sources).cte(
        "candidate_sources"
    )
    source_flags_by_candidate: defaultdict[str, dict[str, str | None]] = defaultdict(
        dict
    )
    for source in connection.execute(
        sa.select(
            candidate_sources.c.candidate_id,
            candidate_sources.c.source_image_id,
            source_images.c.review_flag,
        ).join(
            source_images,
            source_images.c.id == candidate_sources.c.source_image_id,
        )
    ).mappings():
        source_flags_by_candidate[source["candidate_id"]][
            source["source_image_id"]
        ] = source["review_flag"]

    fully_excluded_ids: set[str] = set()
    for candidate in candidate_rows:
        candidate_id = candidate["id"]
        source_flags = source_flags_by_candidate[candidate_id]
        excluded_ids = {
            source_image_id
            for source_image_id, review_flag in source_flags.items()
            if review_flag in EXCLUDED_FLAGS
        }
        next_status = candidate["status"]
        next_flags = _flags(candidate["anomaly_flags_json"])
        if excluded_ids == set(source_flags) and source_flags:
            if next_status not in INACTIVE_STATUSES:
                next_status = "ignored"
            if next_status == "ignored":
                fully_excluded_ids.add(candidate_id)
        elif excluded_ids and next_status not in INACTIVE_STATUSES:
            next_status = "review"
            if not any(flag.get("code") == ANOMALY["code"] for flag in next_flags):
                next_flags.append(dict(ANOMALY))

        if (
            next_status != candidate["status"]
            or next_flags != _flags(candidate["anomaly_flags_json"])
        ):
            connection.execute(
                candidates.update()
                .where(candidates.c.id == candidate_id)
                .values(status=next_status, anomaly_flags_json=next_flags)
            )
        candidate_statuses[candidate_id] = next_status

    primary_candidates = candidates.alias("primary_candidates")
    secondary_candidates = candidates.alias("secondary_candidates")
    pending_relationships = list(
        connection.execute(
            sa.select(
                relationships.c.id,
                relationships.c.batch_id,
                relationships.c.primary_candidate_id,
                relationships.c.secondary_candidate_id,
                primary_candidates.c.batch_id.label("primary_batch_id"),
                primary_candidates.c.status.label("primary_status"),
                secondary_candidates.c.batch_id.label("secondary_batch_id"),
                secondary_candidates.c.status.label("secondary_status"),
            )
            .join(
                primary_candidates,
                primary_candidates.c.id == relationships.c.primary_candidate_id,
            )
            .join(
                secondary_candidates,
                secondary_candidates.c.id == relationships.c.secondary_candidate_id,
            )
            .where(relationships.c.status == "pending")
        ).mappings()
    )
    resolved_relationship_ids: set[str] = set()
    for relationship in pending_relationships:
        primary_excluded = (
            relationship["primary_candidate_id"] in fully_excluded_ids
        )
        secondary_excluded = (
            relationship["secondary_candidate_id"] in fully_excluded_ids
        )
        if not primary_excluded and not secondary_excluded:
            continue
        if primary_excluded and secondary_excluded:
            decision = "source_images_excluded"
        elif primary_excluded:
            decision = "kept_secondary"
        else:
            decision = "kept_primary"
        connection.execute(
            relationships.update()
            .where(relationships.c.id == relationship["id"])
            .values(status="resolved", decision=decision)
        )
        resolved_relationship_ids.add(relationship["id"])
        affected_batch_ids.update(
            {
                relationship["batch_id"],
                relationship["primary_batch_id"],
                relationship["secondary_batch_id"],
            }
        )
        coordinated_candidate_ids.update(
            {
                relationship["primary_candidate_id"],
                relationship["secondary_candidate_id"],
            }
        )

    remaining_pending = [
        relationship
        for relationship in pending_relationships
        if relationship["id"] not in resolved_relationship_ids
    ]
    coordinated_relationship_ids: set[str] = set()
    changed = True
    while changed:
        changed = False
        for relationship in remaining_pending:
            if relationship["id"] in coordinated_relationship_ids:
                continue
            if not (
                relationship["batch_id"] in affected_batch_ids
                or relationship["primary_candidate_id"] in coordinated_candidate_ids
                or relationship["secondary_candidate_id"] in coordinated_candidate_ids
            ):
                continue
            coordinated_relationship_ids.add(relationship["id"])
            affected_batch_ids.update(
                {
                    relationship["batch_id"],
                    relationship["primary_batch_id"],
                    relationship["secondary_batch_id"],
                }
            )
            coordinated_candidate_ids.update(
                {
                    relationship["primary_candidate_id"],
                    relationship["secondary_candidate_id"],
                }
            )
            for role in ("primary", "secondary"):
                candidate_id = relationship[f"{role}_candidate_id"]
                current_status = candidate_statuses.get(
                    candidate_id,
                    relationship[f"{role}_status"],
                )
                if current_status != "ready":
                    continue
                connection.execute(
                    candidates.update()
                    .where(candidates.c.id == candidate_id)
                    .values(status="review")
                )
                candidate_statuses[candidate_id] = "review"
            changed = True

    for batch_id in sorted(affected_batch_ids):
        statuses = list(
            connection.scalars(
                sa.select(candidates.c.status).where(
                    candidates.c.batch_id == batch_id
                )
            )
        )
        found_question_count = sum(
            status not in INACTIVE_STATUSES for status in statuses
        )
        high_confidence_count = sum(status == "ready" for status in statuses)
        review_count = sum(status == "review" for status in statuses)
        ordering_pending = connection.scalar(
            sa.select(ordering_issues.c.id)
            .where(
                ordering_issues.c.batch_id == batch_id,
                ordering_issues.c.status == "pending",
            )
            .limit(1)
        )
        relationship_pending = connection.scalar(
            sa.select(relationships.c.id)
            .where(
                relationships.c.batch_id == batch_id,
                relationships.c.status == "pending",
            )
            .limit(1)
        )
        batch_row = connection.execute(
            sa.select(batches.c.error_count).where(batches.c.id == batch_id)
        ).mappings().one()
        if ordering_pending:
            batch_status = "ordering_review"
        elif review_count or relationship_pending:
            batch_status = "question_review"
        elif batch_row["error_count"]:
            batch_status = "partially_ready"
        else:
            batch_status = "ready"
        connection.execute(
            batches.update()
            .where(batches.c.id == batch_id)
            .values(
                found_question_count=found_question_count,
                high_confidence_count=high_confidence_count,
                review_count=review_count,
                status=batch_status,
            )
        )


def downgrade() -> None:
    # 数据修复不可逆；重新激活候选题会恢复 BUG-007。
    pass
