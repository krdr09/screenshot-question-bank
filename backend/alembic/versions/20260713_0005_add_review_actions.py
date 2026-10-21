"""增加人工复核操作快照。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260713_0005"
down_revision: str | None = "20260713_0004"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("question_candidates", sa.Column("edited_ocr_text", sa.Text(), nullable=True))
    op.create_table(
        "review_actions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("batch_id", sa.String(36), nullable=False),
        sa.Column("candidate_id", sa.String(36), nullable=True),
        sa.Column("target_type", sa.String(30), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("action_type", sa.String(40), nullable=False),
        sa.Column("before_json", sa.JSON(), nullable=False),
        sa.Column("after_json", sa.JSON(), nullable=False),
        sa.Column("undone_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["import_batches.id"]),
        sa.ForeignKeyConstraint(["candidate_id"], ["question_candidates.id"]),
    )
    for column in ("batch_id", "candidate_id", "target_type", "target_id", "action_type"):
        op.create_index(f"ix_review_actions_{column}", "review_actions", [column])


def downgrade() -> None:
    op.drop_table("review_actions")
    op.drop_column("question_candidates", "edited_ocr_text")
