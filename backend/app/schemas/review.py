from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ReviewOption(BaseModel):
    label: str = Field(min_length=1, max_length=1)
    text: str = Field(min_length=1)
    order_index: int | None = None


class ReviewPartUpdate(BaseModel):
    id: str | None = None
    part_index: int = Field(ge=0)
    source_y_start: int = Field(ge=0)
    source_y_end: int = Field(gt=0)


class ReviewCandidateUpdate(BaseModel):
    edited_ocr_text: str | None = None
    source_display_number: str | None = None
    stem: str | None = None
    question_type: Literal["single_choice", "true_false"] | None = None
    options: list[ReviewOption] | None = None
    correct_answer: bool | str | None = None
    true_label: str | None = None
    false_label: str | None = None
    explanation: str | None = None
    display_mode: Literal["text", "image_preferred", "text_first", "image_first"] | None = None
    parts: list[ReviewPartUpdate] | None = None


class RelationshipCropUpdate(BaseModel):
    primary_parts: list[ReviewPartUpdate] = Field(min_length=1)
    secondary_parts: list[ReviewPartUpdate] = Field(min_length=1)


class ConfirmedAction(BaseModel):
    confirmed: Literal[True]


class DuplicateAction(ConfirmedAction):
    duplicate_of_id: str


class MergeAction(ConfirmedAction):
    secondary_candidate_id: str
    relationship_id: str | None = None


class SplitAction(ConfirmedAction):
    split_y: int = Field(gt=0)
    second_stem: str = Field(min_length=1)


class ReorderAction(ConfirmedAction):
    direction: Literal["up", "down"]


class RelationshipAction(ConfirmedAction):
    decision: Literal[
        "confirm", "swap", "only_primary", "only_secondary", "separate", "two_questions", "delete"
    ]
