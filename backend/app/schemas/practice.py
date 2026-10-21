from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, StrictBool, StrictStr


class PracticeSelectionRequest(BaseModel):
    course_id: str | None = None
    category_id: str | None = None
    batch_id: str | None = None
    question_type: Literal["single_choice", "true_false"] | None = None
    membership: Literal["all", "wrong", "unanswered"] = "all"
    manual_question_ids: list[str] = Field(default_factory=list)
    position_start: int | None = Field(default=None, ge=1)
    position_end: int | None = Field(default=None, ge=1)


class PracticeCandidate(BaseModel):
    id: str
    question_type: Literal["single_choice", "true_false"]
    stem: str
    course_id: str
    course_name: str
    category_id: str | None
    category_path: str
    batch_id: str
    batch_name: str
    batch_position: int
    source_display_number: str | None
    in_wrong_book: bool
    has_answered: bool


class PracticeCandidatePage(BaseModel):
    items: list[PracticeCandidate]
    total: int
    page: int
    page_size: int


class PracticeSessionCreate(BaseModel):
    selection: PracticeSelectionRequest = Field(default_factory=PracticeSelectionRequest)
    count: int | None = Field(default=None, ge=1)
    order_mode: Literal["sequential", "random"]
    feedback_mode: Literal["immediate", "deferred"]


class PracticeOption(BaseModel):
    label: str
    text: str
    order_index: int | None = None


class PracticeImagePart(BaseModel):
    part_index: int
    source_y_start: int
    source_y_end: int
    url: str


class PracticeItemResponse(BaseModel):
    id: str
    question_id: str
    order_index: int
    question_type: Literal["single_choice", "true_false"]
    stem: str
    options: list[PracticeOption]
    display_mode: str
    source_display_number: str | None
    course_id: str
    course_name: str
    category_id: str | None
    category_path: str
    batch_id: str
    batch_name: str
    batch_position: int
    image_parts: list[PracticeImagePart]
    selected_answer: bool | str | None = None
    saved: bool
    locked: bool
    is_correct: bool | None = None
    correct_answer: bool | str | None = None
    explanation: str | None = None
    was_unanswered: bool = False


class PracticeSessionResponse(BaseModel):
    id: str
    feedback_mode: Literal["immediate", "deferred"]
    order_mode: Literal["sequential", "random"]
    status: Literal["active", "submitted"]
    requested_count: int | None
    actual_count: int
    limit_adjusted: bool
    notice: str | None = None
    started_at: datetime
    submitted_at: datetime | None
    answered_count: int
    items: list[PracticeItemResponse]


class PracticeSessionHistoryItem(BaseModel):
    id: str
    feedback_mode: Literal["immediate", "deferred"]
    order_mode: Literal["sequential", "random"]
    status: Literal["active", "submitted"]
    actual_count: int
    answered_count: int
    correct_count: int | None
    incorrect_count: int | None
    unanswered_count: int | None
    started_at: datetime
    submitted_at: datetime | None


class PracticeSessionHistoryPage(BaseModel):
    items: list[PracticeSessionHistoryItem]
    total: int
    page: int
    page_size: int


class PracticeAnswerRequest(BaseModel):
    practice_item_id: str
    selected_answer: StrictBool | StrictStr


class PracticeResult(BaseModel):
    session_id: str
    total_count: int
    correct_count: int
    incorrect_count: int
    unanswered_count: int
    accuracy: float
    items: list[PracticeItemResponse]
