from __future__ import annotations

import json
import os
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, ConfigDict, ValidationError, create_model
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined
from pydantic_settings import PydanticBaseSettingsSource


PORTABLE_SETTING_NAMES = (
    "ocr_engine",
    "ocr_device",
    "ocr_confidence_threshold",
    "overlap_band_ratio",
    "overlap_text_weight",
    "overlap_visual_weight",
    "overlap_candidate_threshold",
    "overlap_auto_threshold",
    "overlap_ambiguity_margin",
    "overlap_visual_min_ratio",
    "overlap_visual_max_ratio",
    "overlap_visual_ratio_step",
    "duplicate_text_threshold",
    "duplicate_hash_distance",
    "duplicate_review_margin",
    "duplicate_hash_review_margin",
)

PORTABLE_BACKWARD_COMPATIBLE_DEFAULTS = frozenset(
    {"duplicate_hash_review_margin"}
)


def validate_relationship_setting_values(settings: Any) -> None:
    if settings.overlap_text_weight + settings.overlap_visual_weight <= 0:
        raise ValueError("文字和图像重叠权重不能同时为 0")
    if settings.overlap_visual_min_ratio > settings.overlap_visual_max_ratio:
        raise ValueError("视觉重叠最小比例不能大于最大比例")
    if settings.overlap_candidate_threshold > settings.overlap_auto_threshold:
        raise ValueError("重叠候选阈值不能大于自动判定阈值")
    if settings.duplicate_text_threshold - settings.duplicate_review_margin < 0:
        raise ValueError("重复题文本待检查下界不能小于 0")
    if (
        settings.duplicate_hash_distance
        + settings.duplicate_hash_review_margin
        > 64
    ):
        raise ValueError("重复题感知哈希待检查范围不能超过 64")


def portable_settings_path(data_dir: Path) -> Path:
    return data_dir / "config" / "migratable-settings.json"


def portable_settings_payload(settings: Any) -> dict[str, object]:
    return {name: getattr(settings, name) for name in PORTABLE_SETTING_NAMES}


@lru_cache(maxsize=None)
def _portable_settings_model(settings_cls):
    fields = {}
    for name in PORTABLE_SETTING_NAMES:
        field = deepcopy(settings_cls.model_fields[name])
        field.alias = None
        field.validation_alias = None
        field.serialization_alias = None
        if name not in PORTABLE_BACKWARD_COMPATIBLE_DEFAULTS:
            field.default = PydanticUndefined
        fields[name] = (field.annotation, field)
    return create_model(
        "PortableSettingsPayload",
        __config__=ConfigDict(extra="forbid", strict=True),
        **fields,
    )


def validate_portable_settings_bytes(settings_cls, payload: bytes) -> dict[str, Any]:
    try:
        raw = json.loads(payload)
        if not isinstance(raw, dict):
            raise ValueError("可迁移配置必须是 JSON 对象")
        validated = _portable_settings_model(settings_cls).model_validate(raw)
        validate_relationship_setting_values(validated)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
    ) as error:
        raise ValueError("可迁移配置格式无效") from error
    return validated.model_dump()


class PortableSettingsSource(PydanticBaseSettingsSource):
    def __init__(self, settings_cls, data_dir: Path) -> None:
        super().__init__(settings_cls)
        self.path = portable_settings_path(data_dir)

    def get_field_value(
        self, field: FieldInfo, field_name: str
    ) -> tuple[Any, str, bool]:
        values = self()
        return values.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {}
        values: dict[str, Any] = {}
        for name in PORTABLE_SETTING_NAMES:
            if name not in payload:
                continue
            field = self.settings_cls.model_fields[name]
            alias = field.validation_alias
            key = (
                alias.choices[0]
                if isinstance(alias, AliasChoices)
                else alias or name
            )
            values[str(key)] = payload[name]
        return values


def configured_data_dir(init_settings: PydanticBaseSettingsSource, default: Path) -> Path:
    init_values = init_settings()
    value = init_values.get("data_dir") or os.getenv("QUESTION_PRACTICE_DATA_DIR")
    return Path(value) if value is not None else default
