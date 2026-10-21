import hashlib
import json
from pathlib import Path

from PIL import Image
import pytest

from app.ocr.base import OcrBox, OcrPageResult, Point
from app.ocr.mock import MockOcrEngine, load_mock_manifest
from app.config import Settings
from app.main import create_app


def _result(text: str) -> OcrPageResult:
    return OcrPageResult(
        page_width=64,
        page_height=64,
        boxes=[
            OcrBox(
                text=text,
                polygon=[
                    Point(x=1, y=1),
                    Point(x=63, y=1),
                    Point(x=63, y=20),
                    Point(x=1, y=20),
                ],
                confidence=0.99,
            )
        ],
        raw_output={"engine": "mock", "fixture": "manifest"},
    )


def test_mock_manifest_uses_sha256_after_upload_rename(tmp_path: Path) -> None:
    original = tmp_path / "合成原图.png"
    Image.new("RGB", (64, 64), "white").save(original)
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    manifest = tmp_path / "fixture.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "results_by_sha256": {
                    digest: _result("1.[单选题]").model_dump(
                        mode="json",
                        exclude_computed_fields=True,
                    )
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    renamed = tmp_path / "应用内部随机名称.png"
    renamed.write_bytes(original.read_bytes())

    engine = MockOcrEngine.from_manifest(manifest)

    assert engine.recognize(renamed).boxes[0].text == "1.[单选题]"


def test_mock_manifest_rejects_unknown_fields_and_invalid_hash(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "invalid.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "results_by_sha256": {
                    "not-a-sha256": _result("题目").model_dump(
                        mode="json",
                        exclude_computed_fields=True,
                    )
                },
                "unexpected": True,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Mock OCR fixture"):
        load_mock_manifest(manifest)


def test_mock_manifest_rejects_duplicate_sha256_keys(tmp_path: Path) -> None:
    digest = "a" * 64
    result = json.dumps(
        _result("题目").model_dump(mode="json", exclude_computed_fields=True),
        ensure_ascii=False,
    )
    manifest = tmp_path / "duplicate.json"
    manifest.write_text(
        '{"schema_version":1,"results_by_sha256":'
        f'{{"{digest}":{result},"{digest}":{result}}}'
        "}",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Mock OCR fixture"):
        load_mock_manifest(manifest)


def test_mock_manifest_rejects_unknown_version_and_invalid_result(
    tmp_path: Path,
) -> None:
    unknown_version = tmp_path / "version.json"
    unknown_version.write_text(
        '{"schema_version":2,"results_by_sha256":{}}',
        encoding="utf-8",
    )
    invalid_result = tmp_path / "result.json"
    invalid_result.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "results_by_sha256": {
                    "b" * 64: {
                        "page_width": 0,
                        "page_height": 64,
                        "boxes": [],
                        "raw_output": None,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    for manifest in (unknown_version, invalid_result):
        with pytest.raises(ValueError, match="Mock OCR fixture"):
            load_mock_manifest(manifest)


def test_app_mock_worker_loads_configured_manifest(tmp_path: Path) -> None:
    image = tmp_path / "worker.png"
    Image.new("RGB", (64, 64), "white").save(image)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    manifest = tmp_path / "worker-fixture.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "results_by_sha256": {
                    digest: _result("由独立服务读取").model_dump(
                        mode="json",
                        exclude_computed_fields=True,
                    )
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing-dist",
        OCR_ENGINE="mock",
        mock_ocr_fixture_path=manifest,
        _env_file=None,
    )

    app = create_app(settings)

    result = app.state.ocr_worker.engine.recognize(image)
    assert result.boxes[0].text == "由独立服务读取"
