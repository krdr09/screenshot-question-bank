from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from PIL import Image
from pydantic import BaseModel, Field, model_validator


class CropBounds(BaseModel):
    y_start: int = Field(ge=0)
    y_end: int = Field(gt=0)
    page_height: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self) -> "CropBounds":
        if self.y_start >= self.y_end or self.y_end > self.page_height:
            raise ValueError("裁切纵向范围无效")
        return self


def crop_question_image(source: Path, target: Path, bounds: CropBounds) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".{target.name}.crop-{uuid4().hex}.tmp"
    )
    if len(os.path.abspath(temporary)) >= 260:
        temporary = target.with_name(f".crop-{uuid4().hex}.tmp")
    try:
        with Image.open(source) as image:
            if image.height != bounds.page_height:
                raise ValueError("OCR 页面高度与原图不一致")
            cropped = image.crop((0, bounds.y_start, image.width, bounds.y_end))
            cropped.save(temporary, format="PNG")
        os.replace(temporary, target)
    except Exception as error:
        try:
            temporary.unlink(missing_ok=True)
        except Exception as cleanup_error:
            error.add_note(
                f"Failed to clean temporary crop file {temporary}: {cleanup_error}"
            )
        raise
    else:
        temporary.unlink(missing_ok=True)
