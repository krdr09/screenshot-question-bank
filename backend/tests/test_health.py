from dataclasses import FrozenInstanceError
import json
from logging.handlers import TimedRotatingFileHandler
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import ServiceAvailabilityState, app, create_app
from app.services.backups.recovery import (
    RestoreRecoverySentinel,
    RestoreRecoveryState,
    recovery_public_detail,
)


def test_health_reports_local_service_status() -> None:
    response = TestClient(app).get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "app_name": "截图题库 OCR 与刷题系统",
        "version": "0.1.0",
        "host": "127.0.0.1",
    }


def test_app_lifespan_closes_its_rotating_log_handler(tmp_path) -> None:
    current = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing-dist",
        _env_file=None,
    )
    local_app = create_app(current)

    with TestClient(local_app) as client:
        assert client.get("/api/health").status_code == 200

    leaked = [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, TimedRotatingFileHandler)
        and str(tmp_path) in handler.baseFilename
    ]
    assert leaked == []


def test_recovery_sentinel_blocks_startup_writes_and_reports_health(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'data' / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=True,
        frontend_dist=tmp_path / "missing-dist",
        OCR_ENGINE="mock",
        OCR_DEVICE="cpu",
        _env_file=None,
    )
    sentinel = RestoreRecoverySentinel(settings.data_dir)
    sentinel.arm(
        operation_id="operation-17",
        safety_backup_id="backup-17",
        safety_backup_filename="backup-17.zip",
    )
    staging_residue = settings.data_dir / ".restore-staging" / "must-keep"
    staging_residue.mkdir(parents=True)
    application = create_app(settings)

    with TestClient(application) as client:
        health = client.get("/api/health")
        blocked = client.get("/api/courses")

    assert health.status_code == 200
    assert health.json() == {
        "status": "recovery_required",
        "app_name": settings.app_name,
        "version": settings.app_version,
        "host": settings.host,
        "code": "BACKUP_RECOVERY_REQUIRED",
        "detail": recovery_public_detail(sentinel.load()),
        "safety_backup_id": "backup-17",
        "safety_backup_filename": "backup-17.zip",
    }
    assert blocked.status_code == 503
    assert blocked.json()["code"] == "BACKUP_RECOVERY_REQUIRED"
    assert not application.state.database.sqlite_path().exists()
    assert application.state.ocr_worker._thread is None
    assert staging_residue.exists()


def test_availability_publishes_immutable_snapshot_and_never_downgrades_recovery(
) -> None:
    availability = ServiceAvailabilityState()
    ready = availability.snapshot()
    recovery = RestoreRecoveryState(
        operation_id="operation-17",
        safety_backup_id="backup-17",
        safety_backup_filename="backup-17.zip",
        created_at="2026-08-11T00:00:00+00:00",
    )

    availability.require_recovery(recovery)
    published_recovery = availability.snapshot()
    availability.require_restart()
    after_restart_request = availability.snapshot()

    assert ready.mode == "ready"
    assert ready.recovery is None
    assert published_recovery.mode == "recovery_required"
    assert published_recovery.recovery == recovery
    assert after_restart_request == published_recovery
    with pytest.raises(FrozenInstanceError):
        ready.mode = "restart_required"


def test_inconsistent_availability_snapshot_fails_closed_without_assertions(
    tmp_path: Path,
) -> None:
    current = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'data' / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing-dist",
        _env_file=None,
    )
    application = create_app(current)

    with TestClient(application, raise_server_exceptions=False) as client:
        application.state.service_availability.snapshot = lambda: SimpleNamespace(
            mode="recovery_required", recovery=None
        )
        health = client.get("/api/health")
        blocked = client.get("/api/courses")

    assert health.status_code == 200
    assert health.json()["status"] == "recovery_required"
    assert "safety_backup_id" not in health.json()
    assert "safety_backup_filename" not in health.json()
    assert blocked.status_code == 503
    assert blocked.json()["code"] == "BACKUP_RECOVERY_REQUIRED"


def test_untrusted_sentinel_metadata_is_not_disclosed_by_health(
    tmp_path: Path,
) -> None:
    current = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'data' / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing-dist",
        _env_file=None,
    )
    sentinel = RestoreRecoverySentinel(current.data_dir)
    sentinel.path.parent.mkdir(parents=True)
    sentinel.path.write_text(
        json.dumps(
            {
                "version": 1,
                "operation_id": "../raw-operation-secret",
                "safety_backup_id": "C:\\private\\raw-backup-secret",
                "safety_backup_filename": "../raw-filename-secret.zip",
                "created_at": "2026-08-11T00:00:00",
            }
        ),
        encoding="utf-8",
    )
    application = create_app(current)

    with TestClient(application) as client:
        health = client.get("/api/health")

    assert health.status_code == 200
    assert health.json()["status"] == "recovery_required"
    assert "safety_backup_id" not in health.json()
    assert "safety_backup_filename" not in health.json()
    assert "raw-operation-secret" not in health.text
    assert "raw-backup-secret" not in health.text
    assert "raw-filename-secret" not in health.text


def test_locked_startup_does_not_create_external_sqlite_parent(
    tmp_path: Path,
) -> None:
    external_parent = tmp_path / "external" / "missing-parent"
    current = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(external_parent / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing-dist",
        _env_file=None,
    )
    RestoreRecoverySentinel(current.data_dir).arm(
        operation_id="operation-17",
        safety_backup_id="backup-17",
        safety_backup_filename="backup-17.zip",
    )
    assert not external_parent.exists()

    application = create_app(current)
    with TestClient(application) as client:
        health = client.get("/api/health")

    assert health.status_code == 200
    assert health.json()["status"] == "recovery_required"
    assert not external_parent.exists()


@pytest.mark.parametrize("content", ["not-json", '{"version": 99}'])
def test_invalid_recovery_sentinel_still_blocks_startup(
    tmp_path: Path,
    content: str,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'data' / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=True,
        frontend_dist=tmp_path / "missing-dist",
        OCR_ENGINE="mock",
        OCR_DEVICE="cpu",
        _env_file=None,
    )
    sentinel = RestoreRecoverySentinel(settings.data_dir)
    sentinel.path.parent.mkdir(parents=True)
    sentinel.path.write_text(content, encoding="utf-8")
    application = create_app(settings)

    with TestClient(application) as client:
        health = client.get("/api/health")
        blocked = client.get("/api/courses")

    assert health.status_code == 200
    assert health.json()["status"] == "recovery_required"
    assert "safety_backup_id" not in health.json()
    assert "safety_backup_filename" not in health.json()
    assert blocked.status_code == 503
    assert not application.state.database.sqlite_path().exists()
    assert application.state.ocr_worker._thread is None
