from pathlib import Path

from PIL import Image

from app.testing.synthetic_screenshots import (
    generate_normal_screenshot_set,
    generate_parsing_screenshot_set,
)


def test_generator_creates_three_stable_images_with_matching_mock_ocr(
    tmp_path: Path,
) -> None:
    pages = generate_normal_screenshot_set(tmp_path)

    assert [page.image_path.name for page in pages] == [
        "normal-01.png",
        "normal-02.png",
        "normal-03.png",
    ]
    assert [page.ocr_result.full_text.splitlines()[0] for page in pages] == [
        "Question 1",
        "Question 2",
        "Question 3",
    ]
    for page in pages:
        with Image.open(page.image_path) as image:
            assert image.size == (
                page.ocr_result.page_width,
                page.ocr_result.page_height,
            )
        assert all(box.confidence >= 0.95 for box in page.ocr_result.boxes)


def test_parsing_set_covers_a_to_e_true_false_and_anomaly(tmp_path: Path) -> None:
    pages = generate_parsing_screenshot_set(tmp_path)

    texts = [page.ocr_result.full_text for page in pages]
    assert "E. 戊" in texts[0]
    assert "正确答案:E" in texts[0]
    assert "[判断题]" in texts[1]
    assert "A. 错误" in texts[1] and "B. 正确" in texts[1]
    assert "正确答案" not in texts[2]
