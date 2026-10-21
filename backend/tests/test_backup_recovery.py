from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.backups import recovery
from app.services.backups.recovery import (
    RestoreRecoverySentinel,
    recovery_public_detail,
)


def write_sentinel_payload(path: Path, **overrides: object) -> None:
    payload: dict[str, object] = {
        "version": 1,
        "operation_id": "operation-17",
        "safety_backup_id": "backup-17",
        "safety_backup_filename": "backup-17.zip",
        "created_at": "2026-08-11T00:00:00+00:00",
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_recovery_sentinel_round_trip_and_clear(tmp_path: Path) -> None:
    sentinel = RestoreRecoverySentinel(tmp_path)

    armed = sentinel.arm(
        operation_id="operation-17",
        safety_backup_id="backup-17",
        safety_backup_filename="backup-17.zip",
    )

    assert sentinel.load() == armed
    payload = json.loads(sentinel.path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["operation_id"] == "operation-17"
    assert payload["safety_backup_filename"] == "backup-17.zip"
    assert str(tmp_path.resolve()) not in sentinel.path.read_text(encoding="utf-8")
    assert "backup-17.zip" in recovery_public_detail(armed)

    sentinel.clear()
    assert sentinel.load() is None


@pytest.mark.parametrize(
    "content",
    [
        "not-json",
        '{"version": 99}',
        '{"version": 1, "operation_id": 42}',
        "[]",
        '"sentinel"',
        "null",
    ],
)
def test_invalid_or_unknown_sentinel_fails_closed(
    tmp_path: Path,
    content: str,
) -> None:
    sentinel = RestoreRecoverySentinel(tmp_path)
    sentinel.path.write_text(content, encoding="utf-8")

    state = sentinel.load()

    assert state is not None
    assert state.operation_id is None
    assert state.safety_backup_id is None
    assert state.safety_backup_filename is None
    assert "人工恢复" in recovery_public_detail(state)


def test_non_file_sentinel_fails_closed(tmp_path: Path) -> None:
    sentinel = RestoreRecoverySentinel(tmp_path)
    sentinel.path.mkdir()

    state = sentinel.load()

    assert state is not None
    assert state.operation_id is None
    assert state.safety_backup_id is None
    assert state.safety_backup_filename is None


def test_symlink_sentinel_fails_closed_without_following_target(
    tmp_path: Path,
) -> None:
    sentinel = RestoreRecoverySentinel(tmp_path)
    target = tmp_path / "attacker-controlled.json"
    write_sentinel_payload(target)
    try:
        sentinel.path.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlinks are unavailable: {error}")

    state = sentinel.load()

    assert state is not None
    assert state.operation_id is None
    assert state.safety_backup_id is None
    assert state.safety_backup_filename is None
    assert state.created_at is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operation_id", "../raw-operation-secret"),
        ("operation_id", "operation\nraw-secret"),
        ("safety_backup_id", "C:\\private\\raw-backup-secret"),
        ("safety_backup_id", "backup id with spaces"),
        ("created_at", "2026-08-11T00:00:00"),
        ("created_at", "2026-08-11T08:00:00+08:00"),
        ("created_at", "not-a-timestamp"),
        ("safety_backup_filename", "../raw-filename-secret.zip"),
        ("safety_backup_filename", "nested/raw-filename-secret.zip"),
        ("safety_backup_filename", "nested\\raw-filename-secret.zip"),
        ("safety_backup_filename", "."),
        ("safety_backup_filename", ".."),
    ],
)
def test_untrusted_sentinel_metadata_fails_closed_without_partial_disclosure(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    sentinel = RestoreRecoverySentinel(tmp_path)
    write_sentinel_payload(sentinel.path, **{field: value})

    state = sentinel.load()

    assert state is not None
    assert state.operation_id is None
    assert state.safety_backup_id is None
    assert state.safety_backup_filename is None
    assert state.created_at is None


def test_sentinel_publish_failure_leaves_no_marker_or_temp_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = RestoreRecoverySentinel(tmp_path)

    def fail_replace(source, destination) -> None:
        raise OSError("synthetic sentinel publish failure")

    monkeypatch.setattr(recovery.os, "replace", fail_replace)

    with pytest.raises(OSError, match="sentinel publish failure"):
        sentinel.arm(
            operation_id="operation-17",
            safety_backup_id="backup-17",
            safety_backup_filename="backup-17.zip",
        )

    assert not sentinel.path.exists()
    assert not list(tmp_path.glob(".restore-recovery-required.*.tmp"))
