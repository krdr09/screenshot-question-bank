from pathlib import Path

from app.ocr.base import OcrBox, OcrPageResult, Point
from app.ocr.mock import MockOcrEngine


def page_result(text: str = "Question 1") -> OcrPageResult:
    return OcrPageResult(
        page_width=1080,
        page_height=1920,
        boxes=[
            OcrBox(
                text=text,
                polygon=[
                    Point(x=10, y=20),
                    Point(x=500, y=20),
                    Point(x=500, y=80),
                    Point(x=10, y=80),
                ],
                confidence=0.98,
            )
        ],
        raw_output={"provider": "fixture", "page": 1},
    )


def test_ocr_page_result_keeps_coordinates_confidence_and_raw_output() -> None:
    result = page_result()

    assert result.full_text == "Question 1"
    assert result.boxes[0].polygon[2] == Point(x=500, y=80)
    assert result.boxes[0].confidence == 0.98
    assert result.raw_output == {"provider": "fixture", "page": 1}


def test_mock_ocr_returns_configured_result_without_reading_a_model(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "page.png"
    image_path.write_bytes(b"not-needed-by-mock")
    expected = page_result("Mock page")
    engine = MockOcrEngine(results_by_name={"page.png": expected})

    actual = engine.recognize(image_path)

    assert actual == expected
    assert engine.device == "mock"
    assert engine.calls == [image_path]

