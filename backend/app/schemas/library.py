from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class LibraryOption(BaseModel):
    label: str = Field(min_length=1, max_length=1)
    text: str = Field(min_length=1)
    order_index: int | None = None


class CategorySummary(BaseModel):
    id: str
    path: str


class CourseSummary(BaseModel):
    id: str
    name: str
    categories: list[CategorySummary]


class CourseList(BaseModel):
    items: list[CourseSummary]


class BatchSummary(BaseModel):
    id: str
    name: str
    status: str
    course_id: str
    course_name: str
    category_id: str | None
    category_path: str
    question_count: int


class BatchList(BaseModel):
    items: list[BatchSummary]


class LibraryPart(BaseModel):
    id: str
    source_image_id: str
    part_index: int
    source_y_start: int
    source_y_end: int
    crop_url: str
    source_url: str


class LibraryQuestion(BaseModel):
    id: str
    internal_uuid: str
    batch_id: str
    batch_name: str
    course_id: str
    course_name: str
    category_id: str | None
    category_path: str
    source_image_id: str
    source_image_name: str
    source_display_number: str | None
    question_type: Literal["single_choice", "true_false"]
    stem: str
    options: list[LibraryOption]
    correct_answer: bool | str | None
    true_label: str | None
    false_label: str | None
    explanation: str | None
    raw_ocr_text: str
    edited_ocr_text: str | None
    display_mode: str
    status: str
    in_wrong_book: bool
    parts: list[LibraryPart]


class LibraryQuestionPage(BaseModel):
    items: list[LibraryQuestion]
    total: int
    page: int
    page_size: int


class LibraryQuestionUpdate(BaseModel):
    category_id: str | None = None
    source_display_number: str | None = None
    stem: str | None = None
    question_type: Literal["single_choice", "true_false"] | None = None
    options: list[LibraryOption] | None = None
    correct_answer: bool | str | None = None
    true_label: str | None = None
    false_label: str | None = None
    explanation: str | None = None
    display_mode: Literal["text", "image_preferred", "text_first", "image_first"] | None = None


class ConfirmedLibraryAction(BaseModel):
    confirmed: Literal[True]


class DeleteResult(BaseModel):
    id: str
    deleted: bool


class BatchDeleteResult(DeleteResult):
    name: str
    deleted_questions: int
    deleted_images: int
    cleanup_pending: bool
