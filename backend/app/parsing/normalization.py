from __future__ import annotations

import re
import unicodedata


PUNCTUATION_TRANSLATION = str.maketrans(
    {
        "【": "[",
        "】": "]",
        "「": "[",
        "」": "]",
        "？": "?",
        "！": "!",
        "，": ",",
        "；": ";",
    }
)


def normalize_ocr_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).translate(PUNCTUATION_TRANSLATION)
    normalized = re.sub(r"[\t\r\f\v ]+", " ", normalized).strip()
    normalized = re.sub(r"\s*([:])\s*", r"\1", normalized)
    return normalized
