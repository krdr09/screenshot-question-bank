from __future__ import annotations


PUBLIC_OCR_ERROR_MESSAGE = "OCR 处理失败，请查看本地日志"


def public_ocr_error_message(message: str | None) -> str | None:
    return None if message is None else PUBLIC_OCR_ERROR_MESSAGE
