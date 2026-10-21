from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.imports import SourceImage, new_uuid, utc_now


class QuestionCandidate(Base):
    __tablename__ = "question_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    batch_id: Mapped[str] = mapped_column(ForeignKey("import_batches.id"), index=True)
    category_id: Mapped[str | None] = mapped_column(
        ForeignKey("categories.id"), nullable=True, index=True
    )
    source_image_id: Mapped[str] = mapped_column(
        ForeignKey("source_images.id"), index=True
    )
    source_display_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    question_type: Mapped[str] = mapped_column(String(30), index=True)
    stem: Mapped[str] = mapped_column(Text)
    options_json: Mapped[list[dict]] = mapped_column(JSON, default=list)
    correct_answer_json: Mapped[bool | str | None] = mapped_column(JSON, nullable=True)
    source_correct_answer_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    true_label: Mapped[str | None] = mapped_column(String(1), nullable=True)
    false_label: Mapped[str | None] = mapped_column(String(1), nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_ocr_text: Mapped[str] = mapped_column(Text)
    edited_ocr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_ocr_text: Mapped[str] = mapped_column(Text)
    ocr_confidence: Mapped[float] = mapped_column(Float)
    parse_confidence: Mapped[float] = mapped_column(Float)
    display_mode: Mapped[str] = mapped_column(String(30))
    anomaly_flags_json: Mapped[list[dict]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), index=True)
    duplicate_of_id: Mapped[str | None] = mapped_column(
        ForeignKey("question_candidates.id"), nullable=True, index=True
    )
    content_fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    source_image: Mapped[SourceImage] = relationship(back_populates="candidates")
    image_parts: Mapped[list[CandidateImagePart]] = relationship(
        back_populates="candidate",
        order_by="CandidateImagePart.part_index",
        cascade="all, delete-orphan",
    )
    duplicate_of: Mapped[QuestionCandidate | None] = relationship(
        remote_side="QuestionCandidate.id", foreign_keys=[duplicate_of_id]
    )


class CandidateImagePart(Base):
    __tablename__ = "candidate_image_parts"
    __table_args__ = (UniqueConstraint("candidate_id", "part_index"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("question_candidates.id"), index=True
    )
    source_image_id: Mapped[str] = mapped_column(
        ForeignKey("source_images.id"), index=True
    )
    crop_path: Mapped[str] = mapped_column(String(1000))
    source_y_start: Mapped[int] = mapped_column(Integer)
    source_y_end: Mapped[int] = mapped_column(Integer)
    part_index: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    candidate: Mapped[QuestionCandidate] = relationship(back_populates="image_parts")


class ReviewAction(Base):
    __tablename__ = "review_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    batch_id: Mapped[str] = mapped_column(ForeignKey("import_batches.id"), index=True)
    candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("question_candidates.id"), nullable=True, index=True
    )
    target_type: Mapped[str] = mapped_column(String(30), index=True)
    target_id: Mapped[str] = mapped_column(String(36), index=True)
    action_type: Mapped[str] = mapped_column(String(40), index=True)
    before_json: Mapped[dict] = mapped_column(JSON, default=dict)
    after_json: Mapped[dict] = mapped_column(JSON, default=dict)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WrongBookEntry(Base):
    __tablename__ = "wrong_book_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    question_id: Mapped[str] = mapped_column(
        ForeignKey("question_candidates.id"), unique=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    mastered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
