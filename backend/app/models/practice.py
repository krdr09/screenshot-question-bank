from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.imports import new_uuid, utc_now


class PracticeSession(Base):
    __tablename__ = "practice_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    feedback_mode: Mapped[str] = mapped_column(String(20), index=True)
    order_mode: Mapped[str] = mapped_column(String(20))
    selection_json: Mapped[dict] = mapped_column(JSON, default=dict)
    requested_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_count: Mapped[int] = mapped_column(Integer)
    limit_adjusted: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    items: Mapped[list[PracticeItem]] = relationship(
        back_populates="session",
        order_by="PracticeItem.order_index",
        cascade="all, delete-orphan",
    )
    attempts: Mapped[list[Attempt]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class PracticeItem(Base):
    __tablename__ = "practice_items"
    __table_args__ = (
        UniqueConstraint("session_id", "order_index"),
        UniqueConstraint("session_id", "question_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("practice_sessions.id", ondelete="CASCADE"), index=True
    )
    question_id: Mapped[str | None] = mapped_column(
        ForeignKey("question_candidates.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    order_index: Mapped[int] = mapped_column(Integer)
    question_snapshot_json: Mapped[dict] = mapped_column(JSON)
    answer_snapshot_json: Mapped[bool | str] = mapped_column(JSON)
    explanation_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_parts_snapshot_json: Mapped[list[dict]] = mapped_column(JSON, default=list)

    session: Mapped[PracticeSession] = relationship(back_populates="items")
    attempt: Mapped[Attempt | None] = relationship(
        back_populates="item", uselist=False, cascade="all, delete-orphan"
    )


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (UniqueConstraint("practice_item_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("practice_sessions.id", ondelete="CASCADE"), index=True
    )
    practice_item_id: Mapped[str] = mapped_column(
        ForeignKey("practice_items.id", ondelete="CASCADE"), unique=True, index=True
    )
    selected_answer_json: Mapped[bool | str | None] = mapped_column(JSON, nullable=True)
    is_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    was_unanswered: Mapped[bool] = mapped_column(Boolean, default=False)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    session: Mapped[PracticeSession] = relationship(back_populates="attempts")
    item: Mapped[PracticeItem] = relationship(back_populates="attempt")
