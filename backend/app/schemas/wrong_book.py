from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from app.schemas.practice import PracticeOption


class WrongImagePart(BaseModel):
    part_index: int
    url: str


class WrongQuestion(BaseModel):
    question_id: str
    question_type: Literal["single_choice", "true_false"]
    stem: str
    options: list[PracticeOption]
    correct_answer: bool | str
    explanation: str | None
    course_id: str
    course_name: str
    category_id: str | None
    category_path: str
    batch_id: str
    batch_name: str
    batch_position: int
    source_display_number: str | None
    display_mode: str
    error_count: int
    mastered: bool
    image_parts: list[WrongImagePart]


class WrongQuestionPage(BaseModel):
    items: list[WrongQuestion]
    total: int


class WrongBookMutationResponse(BaseModel):
    question_id: str
    mastered: bool
