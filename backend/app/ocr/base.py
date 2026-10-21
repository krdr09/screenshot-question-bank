from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field, computed_field


class Point(BaseModel):
    x: float
    y: float


class OcrBox(BaseModel):
    text: str
    polygon: list[Point] = Field(min_length=4, max_length=4)
    confidence: float = Field(ge=0, le=1)


class OcrPageResult(BaseModel):
    page_width: int = Field(gt=0)
    page_height: int = Field(gt=0)
    boxes: list[OcrBox]
    raw_output: dict[str, Any] | list[Any] | str | None

    @computed_field
    @property
    def full_text(self) -> str:
        return "\n".join(box.text for box in self.boxes)


class OcrEngine(Protocol):
    device: str

    def recognize(self, image_path: Path) -> OcrPageResult: ...
