from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from app.ocr.base import OcrPageResult


class MockOcrEngine:
    device = "mock"

    def __init__(
        self,
        results_by_name: dict[str, OcrPageResult] | None = None,
        results_by_sha256: dict[str, OcrPageResult] | None = None,
        default: OcrPageResult | None = None,
    ) -> None:
        self.results_by_name = results_by_name or {}
        self.results_by_sha256 = results_by_sha256 or {}
        self.default = default
        self.calls: list[Path] = []

    @classmethod
    def from_manifest(cls, path: Path) -> "MockOcrEngine":
        return cls(results_by_sha256=load_mock_manifest(path))

    def recognize(self, image_path: Path) -> OcrPageResult:
        self.calls.append(image_path)
        result = self.results_by_name.get(image_path.name)
        if result is None and self.results_by_sha256:
            digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
            result = self.results_by_sha256.get(digest)
        if result is None:
            result = self.default
        if result is None:
            raise RuntimeError(f"Mock OCR 没有图片 {image_path.name} 的结果")
        return result.model_copy(deep=True)


def load_mock_manifest(path: Path) -> dict[str, OcrPageResult]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "results_by_sha256",
        }:
            raise ValueError("顶层字段无效")
        if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
            raise ValueError("版本不受支持")
        results = payload["results_by_sha256"]
        if not isinstance(results, dict):
            raise TypeError("results_by_sha256 必须是对象")
        validated: dict[str, OcrPageResult] = {}
        for digest, result in results.items():
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise ValueError("SHA-256 格式无效")
            validated[digest] = OcrPageResult.model_validate(result)
        return validated
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("Mock OCR fixture 格式无效") from error


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 字段重复：{key}")
        result[key] = value
    return result
