"""增加刷题会话、作答历史和错题掌握状态。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260714_0007"
down_revision: str | None = "20260713_0006"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("wrong_book_entries") as batch_op:
        batch_op.add_column(
            sa.Column("mastered_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            )
        )

    op.create_table(
        "practice_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("feedback_mode", sa.String(20), nullable=False),
        sa.Column("order_mode", sa.String(20), nullable=False),
        sa.Column("selection_json", sa.JSON(), nullable=False),
        sa.Column("requested_count", sa.Integer(), nullable=True),
        sa.Column("actual_count", sa.Integer(), nullable=False),
        sa.Column("limit_adjusted", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_practice_sessions_feedback_mode", "practice_sessions", ["feedback_mode"]
    )
    op.create_index("ix_practice_sessions_status", "practice_sessions", ["status"])

    op.create_table(
        "practice_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("question_id", sa.String(36), nullable=True),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("question_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("answer_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("explanation_snapshot", sa.Text(), nullable=True),
        sa.Column("image_parts_snapshot_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"], ["practice_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["question_id"], ["question_candidates.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("session_id", "order_index"),
        sa.UniqueConstraint("session_id", "question_id"),
    )
    op.create_index("ix_practice_items_session_id", "practice_items", ["session_id"])
    op.create_index("ix_practice_items_question_id", "practice_items", ["question_id"])

    op.create_table(
        "attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("practice_item_id", sa.String(36), nullable=False),
        sa.Column("selected_answer_json", sa.JSON(), nullable=True),
        sa.Column("is_correct", sa.Boolean(), nullable=True),
        sa.Column("was_unanswered", sa.Boolean(), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_id"], ["practice_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["practice_item_id"], ["practice_items.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("practice_item_id"),
    )
    op.create_index("ix_attempts_session_id", "attempts", ["session_id"])
    op.create_index(
        "ix_attempts_practice_item_id", "attempts", ["practice_item_id"], unique=True
    )


def downgrade() -> None:
    op.drop_table("attempts")
    op.drop_table("practice_items")
    op.drop_table("practice_sessions")
    with op.batch_alter_table("wrong_book_entries") as batch_op:
        batch_op.drop_column("updated_at")
        batch_op.drop_column("mastered_at")
