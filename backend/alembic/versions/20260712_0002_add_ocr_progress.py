"""增加 OCR 原始结果和持久化任务进度。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260712_0002"
down_revision: str | None = "20260711_0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("import_batches") as batch:
        batch.add_column(
            sa.Column("processed_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column(
                "found_question_count", sa.Integer(), nullable=False, server_default="0"
            )
        )
        batch.add_column(
            sa.Column(
                "high_confidence_count", sa.Integer(), nullable=False, server_default="0"
            )
        )
        batch.add_column(
            sa.Column("review_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("error_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("current_file", sa.String(500), nullable=True))
        batch.add_column(sa.Column("ocr_device", sa.String(20), nullable=True))
        batch.add_column(
            sa.Column(
                "cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch.add_column(
            sa.Column("error_log_json", sa.JSON(), nullable=False, server_default="[]")
        )

    with op.batch_alter_table("source_images") as batch:
        batch.add_column(
            sa.Column(
                "ocr_status", sa.String(30), nullable=False, server_default="pending"
            )
        )
        batch.add_column(sa.Column("ocr_result_json", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("ocr_text", sa.Text(), nullable=True))
        batch.add_column(sa.Column("ocr_error", sa.Text(), nullable=True))
        batch.add_column(sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_source_images_ocr_status", ["ocr_status"])


def downgrade() -> None:
    with op.batch_alter_table("source_images") as batch:
        batch.drop_index("ix_source_images_ocr_status")
        batch.drop_column("processed_at")
        batch.drop_column("ocr_error")
        batch.drop_column("ocr_text")
        batch.drop_column("ocr_result_json")
        batch.drop_column("ocr_status")

    with op.batch_alter_table("import_batches") as batch:
        batch.drop_column("error_log_json")
        batch.drop_column("cancel_requested")
        batch.drop_column("ocr_device")
        batch.drop_column("current_file")
        batch.drop_column("error_count")
        batch.drop_column("review_count")
        batch.drop_column("high_confidence_count")
        batch.drop_column("found_question_count")
        batch.drop_column("processed_count")
