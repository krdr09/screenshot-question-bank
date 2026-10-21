import logging
from pathlib import Path

import pytest
from PIL import Image

from app.ocr.paddle import (
    PaddleOcrEngine,
    RawOcrLine,
    UnavailableOcrEngine,
    create_lazy_ocr_engine,
    create_ocr_engine,
    normalize_paddle_output,
)
from app.ocr.install_check import run_check
from app.ocr.base import OcrPageResult


class FakeBackend:
    def __init__(
        self,
        *,
        cuda: bool = False,
        fail_gpu_smoke: bool = False,
        fail_gpu_recognition: bool = False,
        fail_cuda_probe: bool = False,
        fail_all: bool = False,
    ) -> None:
        self.cuda = cuda
        self.fail_gpu_smoke = fail_gpu_smoke
        self.fail_gpu_recognition = fail_gpu_recognition
        self.fail_cuda_probe = fail_cuda_probe
        self.fail_all = fail_all
        self.create_calls: list[bool] = []

    def cuda_available(self) -> bool:
        if self.fail_cuda_probe:
            raise RuntimeError("CUDA 探测失败")
        return self.cuda

    def create_engine(self, use_gpu: bool):
        self.create_calls.append(use_gpu)
        if self.fail_all:
            raise RuntimeError("模型不存在")
        return {"gpu": use_gpu}

    def smoke_test(self, engine) -> None:
        if engine["gpu"] and self.fail_gpu_smoke:
            raise RuntimeError("GPU 推理失败")

    def recognize(self, engine, image_path: Path) -> list[RawOcrLine]:
        if engine["gpu"] and self.fail_gpu_recognition:
            self.fail_gpu_recognition = False
            raise RuntimeError("GPU 识别显存不足")
        return [
            RawOcrLine(
                text="测试文字",
                confidence=0.96,
                polygon=[[1, 2], [31, 2], [31, 14], [1, 14]],
            )
        ]


def test_cpu_paddle_adapter_returns_standard_page_result(tmp_path: Path) -> None:
    image_path = tmp_path / "page.png"
    Image.new("RGB", (64, 96), "white").save(image_path)
    backend = FakeBackend()
    engine = PaddleOcrEngine(requested_device="cpu", backend=backend)

    result = engine.recognize(image_path)

    assert engine.device == "cpu"
    assert backend.create_calls == [False]
    assert result.page_width == 64
    assert result.page_height == 96
    assert result.full_text == "测试文字"
    assert result.boxes[0].confidence == 0.96
    assert result.boxes[0].polygon[2].x == 31


def test_gpu_smoke_failure_falls_back_to_cpu() -> None:
    backend = FakeBackend(cuda=True, fail_gpu_smoke=True)

    engine = PaddleOcrEngine(requested_device="auto", backend=backend)

    assert engine.device == "cpu"
    assert backend.create_calls == [True, False]
    assert "GPU 推理失败" in (engine.fallback_reason or "")


def test_cuda_probe_exception_falls_back_to_cpu_and_logs(caplog) -> None:
    backend = FakeBackend(fail_cuda_probe=True)

    with caplog.at_level(logging.INFO, logger="app.ocr.paddle"):
        engine = PaddleOcrEngine(requested_device="auto", backend=backend)

    assert engine.device == "cpu"
    assert backend.create_calls == [False]
    assert "CUDA 探测失败" in (engine.fallback_reason or "")
    assert "已回退 CPU" in caplog.text
    assert "PaddleOCR 实际设备：CPU" in caplog.text


def test_gpu_smoke_failure_logs_reason_and_final_device(caplog) -> None:
    backend = FakeBackend(cuda=True, fail_gpu_smoke=True)

    with caplog.at_level(logging.INFO, logger="app.ocr.paddle"):
        PaddleOcrEngine(requested_device="auto", backend=backend)

    assert "GPU 推理失败" in caplog.text
    assert "已回退 CPU" in caplog.text
    assert "PaddleOCR 实际设备：CPU" in caplog.text


def test_cpu_selection_logs_final_device(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="app.ocr.paddle"):
        PaddleOcrEngine(requested_device="cpu", backend=FakeBackend())

    assert "PaddleOCR 实际设备：CPU" in caplog.text


def test_gpu_runtime_failure_retries_current_image_on_cpu(
    tmp_path: Path, caplog
) -> None:
    image_path = tmp_path / "page.png"
    Image.new("RGB", (64, 96), "white").save(image_path)
    backend = FakeBackend(cuda=True, fail_gpu_recognition=True)
    with caplog.at_level(logging.INFO, logger="app.ocr.paddle"):
        engine = PaddleOcrEngine(requested_device="gpu", backend=backend)
        result = engine.recognize(image_path)

    assert result.full_text == "测试文字"
    assert engine.device == "cpu"
    assert backend.create_calls == [True, False]
    assert "GPU 识别显存不足" in (engine.fallback_reason or "")
    assert "GPU 识别显存不足" in caplog.text
    assert "已回退 CPU" in caplog.text
    assert "PaddleOCR 实际设备：CPU" in caplog.text


def test_missing_paddle_model_returns_unavailable_engine_without_startup_failure() -> None:
    engine = create_ocr_engine(
        engine_kind="paddle",
        requested_device="cpu",
        paddle_backend=FakeBackend(fail_all=True),
    )

    assert isinstance(engine, UnavailableOcrEngine)
    assert engine.device == "unavailable"
    with pytest.raises(RuntimeError, match="本地 PaddleOCR 不可用"):
        engine.recognize(Path("page.png"))


def test_normalizes_paddle_3_result_dictionary() -> None:
    lines = normalize_paddle_output(
        {
            "res": {
                "rec_texts": ["第一行", "第二行"],
                "rec_scores": [0.99, 0.88],
                "rec_polys": [
                    [[1, 2], [20, 2], [20, 10], [1, 10]],
                    [[1, 20], [30, 20], [30, 30], [1, 30]],
                ],
            }
        }
    )

    assert [(line.text, line.confidence) for line in lines] == [
        ("第一行", 0.99),
        ("第二行", 0.88),
    ]
    assert lines[1].polygon[2] == [30.0, 30.0]


def test_lazy_paddle_engine_initializes_only_on_first_recognition(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "page.png"
    Image.new("RGB", (64, 96), "white").save(image_path)
    backend = FakeBackend()
    engine = create_lazy_ocr_engine(
        engine_kind="paddle",
        requested_device="cpu",
        paddle_backend=backend,
    )

    assert engine.device == "pending"
    assert backend.create_calls == []

    first = engine.recognize(image_path)
    second = engine.recognize(image_path)

    assert first.full_text == second.full_text == "测试文字"
    assert engine.device == "cpu"
    assert backend.create_calls == [False]


def test_install_check_rejects_a_real_engine_result_without_recognized_boxes() -> None:
    class EmptyResultEngine:
        device = "cpu"

        def recognize(self, image_path: Path) -> OcrPageResult:
            return OcrPageResult(page_width=640, page_height=180, boxes=[], raw_output={})

    with pytest.raises(RuntimeError, match="没有识别到任何文本"):
        run_check(EmptyResultEngine(), "cpu")  # type: ignore[arg-type]
