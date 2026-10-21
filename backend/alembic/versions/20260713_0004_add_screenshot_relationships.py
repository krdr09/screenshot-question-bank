"""增加截图顺序、重复和跨截图关系。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260713_0004"
down_revision: str | None = "20260712_0003"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("source_images", sa.Column("review_flag", sa.String(20), nullable=True))
    with op.batch_alter_table("question_candidates") as batch_op:
        batch_op.add_column(sa.Column("duplicate_of_id", sa.String(36), nullable=True))
        batch_op.add_column(sa.Column("content_fingerprint", sa.String(64), nullable=True))
        batch_op.create_foreign_key(
            "fk_question_candidates_duplicate_of",
            "question_candidates",
            ["duplicate_of_id"],
            ["id"],
        )
        batch_op.create_index("ix_question_candidates_duplicate_of_id", ["duplicate_of_id"])
        batch_op.create_index("ix_question_candidates_content_fingerprint", ["content_fingerprint"])

    op.create_table(
        "screenshot_overlaps",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("batch_id", sa.String(36), nullable=False),
        sa.Column("source_image_id", sa.String(36), nullable=False),
        sa.Column("target_image_id", sa.String(36), nullable=False),
        sa.Column("text_score", sa.Float(), nullable=False),
        sa.Column("visual_score", sa.Float(), nullable=False),
        sa.Column("combined_score", sa.Float(), nullable=False),
        sa.Column("overlap_ratio", sa.Float(), nullable=False),
        sa.Column("matched_text_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["import_batches.id"]),
        sa.ForeignKeyConstraint(["source_image_id"], ["source_images.id"]),
        sa.ForeignKeyConstraint(["target_image_id"], ["source_images.id"]),
        sa.UniqueConstraint("batch_id", "source_image_id", "target_image_id"),
    )
    op.create_index("ix_screenshot_overlaps_batch_id", "screenshot_overlaps", ["batch_id"])
    op.create_index("ix_screenshot_overlaps_source_image_id", "screenshot_overlaps", ["source_image_id"])
    op.create_index("ix_screenshot_overlaps_target_image_id", "screenshot_overlaps", ["target_image_id"])
    op.create_index("ix_screenshot_overlaps_combined_score", "screenshot_overlaps", ["combined_score"])

    op.create_table(
        "ordering_issues",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("batch_id", sa.String(36), nullable=False),
        sa.Column("code", sa.String(40), nullable=False),
        sa.Column("message", sa.String(500), nullable=False),
        sa.Column("image_ids_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["import_batches.id"]),
    )
    op.create_index("ix_ordering_issues_batch_id", "ordering_issues", ["batch_id"])
    op.create_index("ix_ordering_issues_code", "ordering_issues", ["code"])
    op.create_index("ix_ordering_issues_status", "ordering_issues", ["status"])

    op.create_table(
        "candidate_relationships",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("batch_id", sa.String(36), nullable=False),
        sa.Column("primary_candidate_id", sa.String(36), nullable=False),
        sa.Column("secondary_candidate_id", sa.String(36), nullable=False),
        sa.Column("relation_type", sa.String(40), nullable=False),
        sa.Column("decision", sa.String(30), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["import_batches.id"]),
        sa.ForeignKeyConstraint(["primary_candidate_id"], ["question_candidates.id"]),
        sa.ForeignKeyConstraint(["secondary_candidate_id"], ["question_candidates.id"]),
        sa.UniqueConstraint("relation_type", "primary_candidate_id", "secondary_candidate_id"),
    )
    op.create_index("ix_candidate_relationships_batch_id", "candidate_relationships", ["batch_id"])
    op.create_index("ix_candidate_relationships_primary_candidate_id", "candidate_relationships", ["primary_candidate_id"])
    op.create_index("ix_candidate_relationships_secondary_candidate_id", "candidate_relationships", ["secondary_candidate_id"])
    op.create_index("ix_candidate_relationships_relation_type", "candidate_relationships", ["relation_type"])
    op.create_index("ix_candidate_relationships_decision", "candidate_relationships", ["decision"])
    op.create_index("ix_candidate_relationships_status", "candidate_relationships", ["status"])


def downgrade() -> None:
    op.drop_table("candidate_relationships")
    op.drop_table("ordering_issues")
    op.drop_table("screenshot_overlaps")
    with op.batch_alter_table("question_candidates") as batch_op:
        batch_op.drop_index("ix_question_candidates_content_fingerprint")
        batch_op.drop_index("ix_question_candidates_duplicate_of_id")
        batch_op.drop_constraint("fk_question_candidates_duplicate_of", type_="foreignkey")
        batch_op.drop_column("content_fingerprint")
        batch_op.drop_column("duplicate_of_id")
    op.drop_column("source_images", "review_flag")
