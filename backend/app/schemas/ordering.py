from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


ReviewFlag = Literal["duplicate", "unrelated", "damaged"]


class OrderingImageResponse(BaseModel):
    id: str
    original_name: str
    order_index: int
    review_flag: ReviewFlag | None
    duplicate_of_id: str | None
    content_url: str


class OverlapResponse(BaseModel):
    source_image_id: str
    target_image_id: str
    text_score: float
    visual_score: float
    combined_score: float
    overlap_ratio: float
    matched_text: list[str] = Field(default_factory=list)


class OrderingIssueResponse(BaseModel):
    id: str
    code: str
    message: str
    image_ids: list[str]
    status: str


class OrderingResponse(BaseModel):
    batch_id: str
    status: str
    requires_review: bool
    images: list[OrderingImageResponse]
    overlaps: list[OverlapResponse]
    issues: list[OrderingIssueResponse]


class OrderingItemUpdate(BaseModel):
    image_id: str
    review_flag: ReviewFlag | None = None


class OrderingUpdate(BaseModel):
    items: list[OrderingItemUpdate] = Field(min_length=1)
