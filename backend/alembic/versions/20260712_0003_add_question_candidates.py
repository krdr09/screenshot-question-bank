"""增加解析候选题和多图片裁切片段。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260712_0003"
down_revision: str | None = "20260712_0002"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "question_candidates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("batch_id", sa.String(36), nullable=False),
        sa.Column("source_image_id", sa.String(36), nullable=False),
        sa.Column("source_display_number", sa.String(100), nullable=True),
        sa.Column("question_type", sa.String(30), nullable=False),
        sa.Column("stem", sa.Text(), nullable=False),
        sa.Column("options_json", sa.JSON(), nullable=False),
        sa.Column("correct_answer_json", sa.JSON(), nullable=True),
        sa.Column("source_correct_answer_raw", sa.Text(), nullable=True),
        sa.Column("true_label", sa.String(1), nullable=True),
        sa.Column("false_label", sa.String(1), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("raw_ocr_text", sa.Text(), nullable=False),
        sa.Column("normalized_ocr_text", sa.Text(), nullable=False),
        sa.Column("ocr_confidence", sa.Float(), nullable=False),
        sa.Column("parse_confidence", sa.Float(), nullable=False),
        sa.Column("display_mode", sa.String(30), nullable=False),
        sa.Column("anomaly_flags_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["import_batches.id"]),
        sa.ForeignKeyConstraint(["source_image_id"], ["source_images.id"]),
    )
    op.create_index(
        "ix_question_candidates_batch_id", "question_candidates", ["batch_id"]
    )
    op.create_index(
        "ix_question_candidates_source_image_id",
        "question_candidates",
        ["source_image_id"],
    )
    op.create_index(
        "ix_question_candidates_question_type",
        "question_candidates",
        ["question_type"],
    )
    op.create_index(
        "ix_question_candidates_status", "question_candidates", ["status"]
    )

    op.create_table(
        "candidate_image_parts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("candidate_id", sa.String(36), nullable=False),
        sa.Column("source_image_id", sa.String(36), nullable=False),
        sa.Column("crop_path", sa.String(1000), nullable=False),
        sa.Column("source_y_start", sa.Integer(), nullable=False),
        sa.Column("source_y_end", sa.Integer(), nullable=False),
        sa.Column("part_index", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["candidate_id"], ["question_candidates.id"]),
        sa.ForeignKeyConstraint(["source_image_id"], ["source_images.id"]),
        sa.UniqueConstraint("candidate_id", "part_index"),
    )
    op.create_index(
        "ix_candidate_image_parts_candidate_id",
        "candidate_image_parts",
        ["candidate_id"],
    )
    op.create_index(
        "ix_candidate_image_parts_source_image_id",
        "candidate_image_parts",
        ["source_image_id"],
    )


def downgrade() -> None:
    op.drop_table("candidate_image_parts")
    op.drop_table("question_candidates")
