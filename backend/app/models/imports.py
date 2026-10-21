from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def new_uuid() -> str:
    return str(uuid4())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    categories: Mapped[list[Category]] = relationship(back_populates="course")
    batches: Mapped[list[ImportBatch]] = relationship(back_populates="course")


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("course_id", "path"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"), index=True)
    path: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    course: Mapped[Course] = relationship(back_populates="categories")
    batches: Mapped[list[ImportBatch]] = relationship(back_populates="category")


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"), index=True)
    category_id: Mapped[str | None] = mapped_column(
        ForeignKey("categories.id"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    source_relative_path: Mapped[str] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(40), default="created", index=True)
    uploaded_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    found_question_count: Mapped[int] = mapped_column(Integer, default=0)
    high_confidence_count: Mapped[int] = mapped_column(Integer, default=0)
    review_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    current_file: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ocr_device: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    error_log_json: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    course: Mapped[Course] = relationship(back_populates="batches")
    category: Mapped[Category | None] = relationship(back_populates="batches")
    images: Mapped[list[SourceImage]] = relationship(
        back_populates="batch", order_by="SourceImage.order_index"
    )


class SourceImage(Base):
    __tablename__ = "source_images"
    __table_args__ = (
        UniqueConstraint("batch_id", "source_relative_path"),
        Index("ix_source_images_sha256", "sha256"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    batch_id: Mapped[str] = mapped_column(ForeignKey("import_batches.id"), index=True)
    source_relative_path: Mapped[str] = mapped_column(String(1000))
    original_name: Mapped[str] = mapped_column(String(500))
    safe_name: Mapped[str] = mapped_column(String(80))
    relative_path: Mapped[str] = mapped_column(String(1000))
    sha256: Mapped[str] = mapped_column(String(64))
    mime_type: Mapped[str] = mapped_column(String(40))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    file_size: Mapped[int] = mapped_column(Integer)
    duplicate_of_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_images.id"), nullable=True
    )
    review_flag: Mapped[str | None] = mapped_column(String(20), nullable=True)
    order_index: Mapped[int] = mapped_column(Integer)
    ocr_status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    ocr_result_json: Mapped[dict | list | str | None] = mapped_column(JSON, nullable=True)
    ocr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    ocr_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    batch: Mapped[ImportBatch] = relationship(back_populates="images")
    duplicate_of: Mapped[SourceImage | None] = relationship(
        remote_side="SourceImage.id", foreign_keys=[duplicate_of_id]
    )
    candidates: Mapped[list["QuestionCandidate"]] = relationship(
        back_populates="source_image", cascade="all, delete-orphan"
    )
