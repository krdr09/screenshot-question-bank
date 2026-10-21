import os
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw
import pytest

import app.cropping.questions as cropping_questions
from app.cropping.questions import CropBounds, crop_question_image


def test_crop_stops_before_correct_answer_region(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    target = tmp_path / "crops" / "question.png"
    image = Image.new("RGB", (200, 400), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 199, 249), fill="green")
    draw.rectangle((0, 250, 199, 399), fill="red")
    image.save(source)

    crop_question_image(
        source,
        target,
        CropBounds(y_start=20, y_end=242, page_height=400),
    )

    with Image.open(target) as cropped:
        assert cropped.size == (200, 222)
        assert all(pixel[0] < 100 for pixel in cropped.getdata())


@pytest.mark.parametrize("existing_content", [None, b"old-crop"])
def test_crop_save_failure_cleans_temporary_and_preserves_target(
    tmp_path: Path,
    monkeypatch,
    existing_content: bytes | None,
) -> None:
    source = tmp_path / "source.png"
    target = tmp_path / "crops" / "question.png"
    Image.new("RGB", (40, 60), "white").save(source)
    if existing_content is not None:
        target.parent.mkdir(parents=True)
        target.write_bytes(existing_content)

    def fail_after_partial_write(self, fp, *args, **kwargs):
        Path(fp).write_bytes(b"partial")
        raise OSError("crop write interrupted")

    monkeypatch.setattr(Image.Image, "save", fail_after_partial_write)

    with pytest.raises(OSError, match="crop write interrupted"):
        crop_question_image(
            source,
            target,
            CropBounds(y_start=0, y_end=40, page_height=60),
        )

    if existing_content is None:
        assert not target.exists()
    else:
        assert target.read_bytes() == existing_content
    assert list(target.parent.glob(f".{target.name}.crop-*.tmp")) == []


def test_crop_replace_failure_preserves_target_and_cleans_temporary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.png"
    target = tmp_path / "crops" / "question.png"
    Image.new("RGB", (40, 60), "white").save(source)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old-crop")

    def fail_replace(source_path, target_path) -> None:
        assert Path(target_path) == target
        raise OSError("replace interrupted")

    monkeypatch.setattr(
        cropping_questions,
        "os",
        SimpleNamespace(
            path=SimpleNamespace(abspath=os.path.abspath),
            replace=fail_replace,
        ),
        raising=False,
    )

    with pytest.raises(OSError, match="replace interrupted"):
        crop_question_image(
            source,
            target,
            CropBounds(y_start=0, y_end=40, page_height=60),
        )

    assert target.read_bytes() == b"old-crop"
    assert list(target.parent.glob(f".{target.name}.crop-*.tmp")) == []


def test_crop_save_failure_keeps_original_error_when_cleanup_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.png"
    target = tmp_path / "crops" / "question.png"
    Image.new("RGB", (40, 60), "white").save(source)
    original_unlink = Path.unlink

    def fail_after_partial_write(self, fp, *args, **kwargs):
        Path(fp).write_bytes(b"partial")
        raise OSError("crop write interrupted")

    def fail_temporary_cleanup(self, *args, **kwargs):
        if ".crop-" in self.name:
            raise PermissionError("temporary cleanup interrupted")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", fail_after_partial_write)
    monkeypatch.setattr(Path, "unlink", fail_temporary_cleanup)

    with pytest.raises(OSError, match="crop write interrupted") as captured:
        crop_question_image(
            source,
            target,
            CropBounds(y_start=0, y_end=40, page_height=60),
        )

    assert any(
        "temporary cleanup interrupted" in note
        for note in captured.value.__notes__
    )
    assert list(target.parent.glob(f".{target.name}.crop-*.tmp")) != []


def test_relative_target_uses_short_temporary_for_long_absolute_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    source = tmp_path / "source.png"
    target = Path("crops") / "question.png"
    Image.new("RGB", (40, 60), "white").save(source)
    target.parent.mkdir()
    target.write_bytes(b"old-crop")
    original_abspath = os.path.abspath

    def force_long_absolute_path(path):
        if Path(path).name.startswith(f".{target.name}.crop-"):
            return "C:\\" + "x" * 260
        return original_abspath(path)

    def reject_long_temporary_then_partially_write_short(self, fp, *args, **kwargs):
        path = Path(fp)
        if path.name.startswith(".crop-"):
            path.write_bytes(b"partial")
            raise OSError("crop write interrupted")
        raise OSError("long temporary name selected")

    monkeypatch.setattr(cropping_questions.os.path, "abspath", force_long_absolute_path)
    monkeypatch.setattr(
        Image.Image,
        "save",
        reject_long_temporary_then_partially_write_short,
    )

    with pytest.raises(OSError, match="crop write interrupted"):
        crop_question_image(
            source,
            target,
            CropBounds(y_start=0, y_end=40, page_height=60),
        )

    assert target.read_bytes() == b"old-crop"
    assert list(target.parent.glob(f".{target.name}.crop-*.tmp")) == []
    assert list(target.parent.glob(".crop-*.tmp")) == []

