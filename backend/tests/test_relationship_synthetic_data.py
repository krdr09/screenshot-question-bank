import hashlib
from pathlib import Path

from app.testing.synthetic_screenshots import generate_relationship_screenshot_set


def test_relationship_generator_covers_overlap_cross_screenshot_and_near_duplicate(
    tmp_path: Path,
) -> None:
    pages = generate_relationship_screenshot_set(tmp_path)

    assert [page.image_path.name for page in pages] == [
        "cross-random-z.png",
        "cross-random-a.png",
        "duplicate-left.png",
        "duplicate-right.png",
    ]
    assert {box.text for box in pages[0].ocr_result.boxes} >= {"题干继续", "最后一行"}
    assert {box.text for box in pages[1].ocr_result.boxes} >= {"题干继续", "最后一行"}
    assert pages[2].ocr_result.full_text == pages[3].ocr_result.full_text
    assert hashlib.sha256(pages[2].image_path.read_bytes()).digest() != hashlib.sha256(pages[3].image_path.read_bytes()).digest()
