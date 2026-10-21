from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.imports import new_uuid, utc_now


class ScreenshotOverlap(Base):
    __tablename__ = "screenshot_overlaps"
    __table_args__ = (UniqueConstraint("batch_id", "source_image_id", "target_image_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    batch_id: Mapped[str] = mapped_column(ForeignKey("import_batches.id"), index=True)
    source_image_id: Mapped[str] = mapped_column(ForeignKey("source_images.id"), index=True)
    target_image_id: Mapped[str] = mapped_column(ForeignKey("source_images.id"), index=True)
    text_score: Mapped[float] = mapped_column(Float)
    visual_score: Mapped[float] = mapped_column(Float)
    combined_score: Mapped[float] = mapped_column(Float, index=True)
    overlap_ratio: Mapped[float] = mapped_column(Float)
    matched_text_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class OrderingIssue(Base):
    __tablename__ = "ordering_issues"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    batch_id: Mapped[str] = mapped_column(ForeignKey("import_batches.id"), index=True)
    code: Mapped[str] = mapped_column(String(40), index=True)
    message: Mapped[str] = mapped_column(String(500))
    image_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CandidateRelationship(Base):
    __tablename__ = "candidate_relationships"
    __table_args__ = (
        UniqueConstraint("relation_type", "primary_candidate_id", "secondary_candidate_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    batch_id: Mapped[str] = mapped_column(ForeignKey("import_batches.id"), index=True)
    primary_candidate_id: Mapped[str] = mapped_column(ForeignKey("question_candidates.id"), index=True)
    secondary_candidate_id: Mapped[str] = mapped_column(ForeignKey("question_candidates.id"), index=True)
    relation_type: Mapped[str] = mapped_column(String(40), index=True)
    decision: Mapped[str] = mapped_column(String(30), index=True)
    confidence: Mapped[float] = mapped_column(Float)
    evidence_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
