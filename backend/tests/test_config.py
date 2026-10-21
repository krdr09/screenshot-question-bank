import json

import pytest

from app.config import Settings
from app.portable_settings import (
    portable_settings_payload,
    validate_portable_settings_bytes,
)


def test_server_defaults_to_loopback_only() -> None:
    settings = Settings(_env_file=None)

    assert settings.host == "127.0.0.1"
    assert settings.port == 8765


def test_server_rejects_non_loopback_binding() -> None:
    try:
        Settings(host="0.0.0.0", _env_file=None)
    except ValueError as error:
        assert "127.0.0.1" in str(error)
    else:
        raise AssertionError("非本地监听地址必须被拒绝")


def test_installed_application_defaults_to_local_paddle_with_auto_device() -> None:
    settings = Settings(_env_file=None)

    assert settings.ocr_engine == "paddle"
    assert settings.ocr_device == "auto"


def test_short_ocr_environment_variables_are_supported(monkeypatch) -> None:
    monkeypatch.setenv("OCR_ENGINE", "mock")
    monkeypatch.setenv("OCR_DEVICE", "cpu")

    settings = Settings(_env_file=None)

    assert settings.ocr_engine == "mock"
    assert settings.ocr_device == "cpu"


def test_ocr_confidence_threshold_defaults_and_environment_aliases(monkeypatch) -> None:
    monkeypatch.delenv("OCR_CONFIDENCE_THRESHOLD", raising=False)
    monkeypatch.delenv(
        "QUESTION_PRACTICE_OCR_CONFIDENCE_THRESHOLD", raising=False
    )
    assert Settings(_env_file=None).ocr_confidence_threshold == 0.98

    monkeypatch.setenv("OCR_CONFIDENCE_THRESHOLD", "0.8")
    assert Settings(_env_file=None).ocr_confidence_threshold == 0.8

    monkeypatch.setenv("OCR_CONFIDENCE_THRESHOLD", "0.91")
    assert Settings(_env_file=None).ocr_confidence_threshold == 0.91

    monkeypatch.delenv("OCR_CONFIDENCE_THRESHOLD")
    monkeypatch.setenv("QUESTION_PRACTICE_OCR_CONFIDENCE_THRESHOLD", "0.73")
    assert Settings(_env_file=None).ocr_confidence_threshold == 0.73


@pytest.mark.parametrize("value", ["0", "1"])
def test_ocr_confidence_threshold_accepts_inclusive_boundaries(
    value: str, monkeypatch
) -> None:
    monkeypatch.setenv("OCR_CONFIDENCE_THRESHOLD", value)

    assert Settings(_env_file=None).ocr_confidence_threshold == float(value)


@pytest.mark.parametrize("value", ["-0.01", "1.01"])
def test_ocr_confidence_threshold_rejects_out_of_range(
    value: str, monkeypatch
) -> None:
    monkeypatch.setenv("OCR_CONFIDENCE_THRESHOLD", value)

    with pytest.raises(ValueError):
        Settings(_env_file=None)


def test_backup_security_defaults_are_bounded() -> None:
    current = Settings(_env_file=None)

    assert current.max_backup_upload_bytes == 4 * 1024**3
    assert current.max_backup_entries == 10_000
    assert current.max_backup_member_bytes == 4 * 1024**3
    assert current.max_backup_uncompressed_bytes == 8 * 1024**3
    assert current.max_backup_compression_ratio == 200.0
    assert current.max_backup_path_length == 500
    assert current.backup_token_ttl_seconds == 1_800


@pytest.mark.parametrize(
    "kwargs",
    [
        {"overlap_text_weight": 0, "overlap_visual_weight": 0},
        {"overlap_visual_ratio_step": 0},
        {"overlap_visual_min_ratio": 0.5, "overlap_visual_max_ratio": 0.4},
        {"overlap_candidate_threshold": 0.9, "overlap_auto_threshold": 0.8},
        {"duplicate_text_threshold": 0.01, "duplicate_review_margin": 0.02},
        {"duplicate_hash_distance": 63, "duplicate_hash_review_margin": 2},
    ],
)
def test_relationship_settings_reject_invalid_combinations(kwargs) -> None:
    with pytest.raises(ValueError):
        Settings(**kwargs, _env_file=None)


def test_duplicate_hash_review_margin_defaults_and_environment_override(
    monkeypatch,
) -> None:
    monkeypatch.delenv(
        "QUESTION_PRACTICE_DUPLICATE_HASH_REVIEW_MARGIN", raising=False
    )
    assert Settings(_env_file=None).duplicate_hash_review_margin == 2

    monkeypatch.setenv("QUESTION_PRACTICE_DUPLICATE_HASH_REVIEW_MARGIN", "4")
    assert Settings(_env_file=None).duplicate_hash_review_margin == 4


def test_portable_validation_defaults_only_new_hash_review_margin() -> None:
    payload = portable_settings_payload(Settings(_env_file=None))
    payload.pop("duplicate_hash_review_margin")

    validated = validate_portable_settings_bytes(
        Settings,
        json.dumps(payload).encode("utf-8"),
    )
    assert validated["duplicate_hash_review_margin"] == 2

    payload.pop("ocr_device")
    with pytest.raises(ValueError):
        validate_portable_settings_bytes(
            Settings,
            json.dumps(payload).encode("utf-8"),
        )


def test_portable_validation_rejects_relationship_invariants() -> None:
    payload = portable_settings_payload(Settings(_env_file=None))
    payload["overlap_text_weight"] = 0
    payload["overlap_visual_weight"] = 0

    with pytest.raises(ValueError):
        validate_portable_settings_bytes(
            Settings,
            json.dumps(payload).encode("utf-8"),
        )


def test_portable_settings_load_below_environment_and_exclude_machine_paths(
    tmp_path, monkeypatch
) -> None:
    data_dir = tmp_path / "data"
    config_path = data_dir / "config" / "migratable-settings.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '{"ocr_device":"cpu","ocr_confidence_threshold":0.72,'
        '"duplicate_hash_review_margin":3,'
        '"host":"0.0.0.0","data_dir":"C:/unsafe"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("QUESTION_PRACTICE_DATA_DIR", str(data_dir))
    monkeypatch.delenv("OCR_DEVICE", raising=False)
    monkeypatch.delenv("QUESTION_PRACTICE_OCR_DEVICE", raising=False)

    restored = Settings(_env_file=None)

    assert restored.data_dir == data_dir
    assert restored.ocr_device == "cpu"
    assert restored.ocr_confidence_threshold == 0.72
    assert restored.duplicate_hash_review_margin == 3
    assert restored.host == "127.0.0.1"

    monkeypatch.setenv("OCR_DEVICE", "gpu")
    overridden = Settings(_env_file=None)

    assert overridden.ocr_device == "gpu"

    monkeypatch.setenv("OCR_CONFIDENCE_THRESHOLD", "0.93")
    overridden = Settings(_env_file=None)

    assert overridden.ocr_confidence_threshold == 0.93

    monkeypatch.setenv("QUESTION_PRACTICE_DUPLICATE_HASH_REVIEW_MARGIN", "4")
    overridden = Settings(_env_file=None)

    assert overridden.duplicate_hash_review_margin == 4
