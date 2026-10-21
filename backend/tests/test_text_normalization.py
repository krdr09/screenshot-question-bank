from app.parsing.normalization import normalize_ocr_text


def test_normalizes_full_width_letters_digits_and_punctuation() -> None:
    assert normalize_ocr_text("２．【单选题】　Ａ：苹果") == "2.[单选题] A:苹果"

