from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path
from typing import Any, Literal, Protocol

from PIL import Image
from pydantic import BaseModel, Field

from app.ocr.base import OcrBox, OcrEngine, OcrPageResult, Point
from app.ocr.mock import MockOcrEngine


logger = logging.getLogger(__name__)


class RawOcrLine(BaseModel):
    text: str
    confidence: float = Field(ge=0, le=1)
    polygon: list[list[float]] = Field(min_length=4, max_length=4)


class PaddleBackend(Protocol):
    def cuda_available(self) -> bool: ...

    def create_engine(self, use_gpu: bool) -> Any: ...

    def smoke_test(self, engine: Any) -> None: ...

    def recognize(self, engine: Any, image_path: Path) -> list[RawOcrLine]: ...


class PaddleRuntime:
    """PaddleOCR 3.x 的延迟加载边界；导入失败不会影响 Mock 模式启动。"""

    def cuda_available(self) -> bool:
        import paddle

        return bool(
            paddle.device.is_compiled_with_cuda()
            and paddle.device.cuda.device_count() > 0
        )

    def create_engine(self, use_gpu: bool) -> Any:
        from paddleocr import PaddleOCR

        return PaddleOCR(
            lang="ch",
            device="gpu:0" if use_gpu else "cpu",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )

    def smoke_test(self, engine: Any) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temporary:
                temporary_path = Path(temporary.name)
            Image.new("RGB", (32, 32), "white").save(temporary_path, format="PNG")
            self.recognize(engine, temporary_path)
        finally:
            if temporary_path:
                temporary_path.unlink(missing_ok=True)

    def recognize(self, engine: Any, image_path: Path) -> list[RawOcrLine]:
        if hasattr(engine, "predict"):
            raw = list(engine.predict(str(image_path)))
        else:
            raw = engine.ocr(str(image_path), cls=True)
        return normalize_paddle_output(raw)


class PaddleOcrEngine:
    def __init__(
        self,
        requested_device: Literal["auto", "cpu", "gpu"],
        backend: PaddleBackend | None = None,
    ) -> None:
        self.backend = backend or PaddleRuntime()
        self.fallback_reason: str | None = None
        self._engine: Any
        wants_gpu = requested_device in {"auto", "gpu"}
        cuda_available = False
        if wants_gpu:
            try:
                cuda_available = self.backend.cuda_available()
            except Exception as error:
                self.fallback_reason = f"CUDA 可用性探测失败，已回退 CPU：{error}"
                logger.warning("%s", self.fallback_reason)

        if cuda_available:
            try:
                candidate = self.backend.create_engine(use_gpu=True)
                self.backend.smoke_test(candidate)
                self._engine = candidate
                self.device = "gpu"
                logger.info("PaddleOCR 实际设备：GPU")
                return
            except Exception as error:
                self.fallback_reason = f"GPU 初始化或推理自检失败，已回退 CPU：{error}"
                logger.warning("%s", self.fallback_reason)
        elif requested_device == "gpu" and self.fallback_reason is None:
            self.fallback_reason = "未检测到可用 CUDA，已回退 CPU"
            logger.warning("%s", self.fallback_reason)

        self._engine = self.backend.create_engine(use_gpu=False)
        self.backend.smoke_test(self._engine)
        self.device = "cpu"
        logger.info("PaddleOCR 实际设备：CPU")

    def recognize(self, image_path: Path) -> OcrPageResult:
        with Image.open(image_path) as image:
            width, height = image.size
        try:
            lines = self.backend.recognize(self._engine, image_path)
        except Exception as error:
            if self.device != "gpu":
                raise
            self.fallback_reason = f"GPU 识别失败，已回退 CPU：{error}"
            logger.warning("%s", self.fallback_reason)
            self._engine = self.backend.create_engine(use_gpu=False)
            self.backend.smoke_test(self._engine)
            self.device = "cpu"
            logger.info("PaddleOCR 实际设备：CPU")
            lines = self.backend.recognize(self._engine, image_path)
        return OcrPageResult(
            page_width=width,
            page_height=height,
            boxes=[
                OcrBox(
                    text=line.text,
                    confidence=line.confidence,
                    polygon=[Point(x=point[0], y=point[1]) for point in line.polygon],
                )
                for line in lines
            ],
            raw_output={
                "engine": "paddleocr",
                "device": self.device,
                "lines": [line.model_dump(mode="json") for line in lines],
            },
        )


class UnavailableOcrEngine:
    device = "unavailable"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def recognize(self, image_path: Path) -> OcrPageResult:
        raise RuntimeError(f"本地 PaddleOCR 不可用：{self.reason}")


class LazyOcrEngine:
    def __init__(self, factory) -> None:
        self._factory = factory
        self._delegate: OcrEngine | None = None
        self._lock = threading.Lock()

    @property
    def device(self) -> str:
        return self._delegate.device if self._delegate else "pending"

    def recognize(self, image_path: Path) -> OcrPageResult:
        if self._delegate is None:
            with self._lock:
                if self._delegate is None:
                    self._delegate = self._factory()
        return self._delegate.recognize(image_path)


def create_ocr_engine(
    engine_kind: Literal["mock", "paddle"],
    requested_device: Literal["auto", "cpu", "gpu"],
    paddle_backend: PaddleBackend | None = None,
    mock_fixture_path: Path | None = None,
) -> OcrEngine:
    if engine_kind == "mock":
        return (
            MockOcrEngine.from_manifest(mock_fixture_path)
            if mock_fixture_path is not None
            else MockOcrEngine()
        )
    try:
        return PaddleOcrEngine(requested_device, paddle_backend)
    except Exception as error:
        return UnavailableOcrEngine(str(error))


def create_lazy_ocr_engine(
    engine_kind: Literal["mock", "paddle"],
    requested_device: Literal["auto", "cpu", "gpu"],
    paddle_backend: PaddleBackend | None = None,
    mock_fixture_path: Path | None = None,
) -> OcrEngine:
    if engine_kind == "mock":
        return (
            MockOcrEngine.from_manifest(mock_fixture_path)
            if mock_fixture_path is not None
            else MockOcrEngine()
        )
    return LazyOcrEngine(
        lambda: create_ocr_engine(
            engine_kind,
            requested_device,
            paddle_backend,
            mock_fixture_path,
        )
    )


def normalize_paddle_output(raw: Any) -> list[RawOcrLine]:
    dictionaries = _collect_result_dicts(raw)
    lines: list[RawOcrLine] = []
    for dictionary in dictionaries:
        payload = dictionary.get("res", dictionary)
        texts = payload.get("rec_texts", [])
        scores = payload.get("rec_scores", [])
        polygons = payload.get("rec_polys", payload.get("dt_polys", []))
        for text, score, polygon in zip(texts, scores, polygons, strict=False):
            lines.append(
                RawOcrLine(
                    text=str(text),
                    confidence=float(score),
                    polygon=[[float(point[0]), float(point[1])] for point in polygon],
                )
            )
    if lines:
        return lines
    return _normalize_legacy_lines(raw)


def _collect_result_dicts(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        return [raw]
    dictionaries: list[dict[str, Any]] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, dict):
                dictionaries.append(item)
                continue
            value = getattr(item, "json", None)
            if callable(value):
                value = value()
            if isinstance(value, dict):
                dictionaries.append(value)
    return dictionaries


def _normalize_legacy_lines(raw: Any) -> list[RawOcrLine]:
    lines: list[RawOcrLine] = []
    pages = raw if isinstance(raw, (list, tuple)) else []
    for page in pages:
        if not isinstance(page, (list, tuple)):
            continue
        for item in page:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            polygon, recognition = item
            if not isinstance(recognition, (list, tuple)) or len(recognition) != 2:
                continue
            lines.append(
                RawOcrLine(
                    text=str(recognition[0]),
                    confidence=float(recognition[1]),
                    polygon=[[float(point[0]), float(point[1])] for point in polygon],
                )
            )
    return lines
