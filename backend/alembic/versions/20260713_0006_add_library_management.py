"""增加正式题库分类和错题本成员关系。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260713_0006"
down_revision: str | None = "20260713_0005"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("question_candidates") as batch_op:
        batch_op.add_column(sa.Column("category_id", sa.String(36), nullable=True))
        batch_op.create_foreign_key(
            "fk_question_candidates_category_id",
            "categories",
            ["category_id"],
            ["id"],
        )
        batch_op.create_index("ix_question_candidates_category_id", ["category_id"])
    op.execute(
        """
        UPDATE question_candidates
        SET category_id = (
            SELECT import_batches.category_id
            FROM import_batches
            WHERE import_batches.id = question_candidates.batch_id
        )
        """
    )
    op.create_table(
        "wrong_book_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("question_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["question_id"], ["question_candidates.id"]),
        sa.UniqueConstraint("question_id"),
    )
    op.create_index(
        "ix_wrong_book_entries_question_id",
        "wrong_book_entries",
        ["question_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("wrong_book_entries")
    with op.batch_alter_table("question_candidates") as batch_op:
        batch_op.drop_index("ix_question_candidates_category_id")
        batch_op.drop_constraint("fk_question_candidates_category_id", type_="foreignkey")
        batch_op.drop_column("category_id")
