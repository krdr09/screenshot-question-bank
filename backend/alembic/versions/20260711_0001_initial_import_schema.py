"""创建课程、分类、导入批次和来源图片表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260711_0001"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "courses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_courses_name", "courses", ["name"], unique=True)

    op.create_table(
        "categories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("course_id", sa.String(36), nullable=False),
        sa.Column("path", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"]),
        sa.UniqueConstraint("course_id", "path"),
    )
    op.create_index("ix_categories_course_id", "categories", ["course_id"])

    op.create_table(
        "import_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("course_id", sa.String(36), nullable=False),
        sa.Column("category_id", sa.String(36), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("source_relative_path", sa.String(1000), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("uploaded_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"]),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"]),
    )
    op.create_index("ix_import_batches_course_id", "import_batches", ["course_id"])
    op.create_index(
        "ix_import_batches_category_id", "import_batches", ["category_id"]
    )
    op.create_index("ix_import_batches_status", "import_batches", ["status"])

    op.create_table(
        "source_images",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("batch_id", sa.String(36), nullable=False),
        sa.Column("source_relative_path", sa.String(1000), nullable=False),
        sa.Column("original_name", sa.String(500), nullable=False),
        sa.Column("safe_name", sa.String(80), nullable=False),
        sa.Column("relative_path", sa.String(1000), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("mime_type", sa.String(40), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("duplicate_of_id", sa.String(36), nullable=True),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["import_batches.id"]),
        sa.ForeignKeyConstraint(["duplicate_of_id"], ["source_images.id"]),
        sa.UniqueConstraint("batch_id", "source_relative_path"),
    )
    op.create_index("ix_source_images_batch_id", "source_images", ["batch_id"])
    op.create_index("ix_source_images_sha256", "source_images", ["sha256"])


def downgrade() -> None:
    op.drop_table("source_images")
    op.drop_table("import_batches")
    op.drop_table("categories")
    op.drop_table("courses")

