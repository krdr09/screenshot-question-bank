from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path, PureWindowsPath
import threading
from uuid import UUID
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from fastapi.responses import FileResponse
from sqlalchemy import text

from app.api import library as library_api
from app.config import Settings
from app.main import create_app
from app.services.backups import manager as backup_manager_module
from app.services.backups.errors import BackupBusyError, BackupValidationError
from app.services.backups.manager import BackupManager
from app.services.backups.restore import FileOperations
from app.services.backups.validation import ArchiveValidator


@contextmanager
def backup_client(tmp_path: Path, **setting_overrides):
    data_dir = tmp_path / "data"
    values = {
        "data_dir": data_dir,
        "database_url": f"sqlite:///{(data_dir / 'app.db').as_posix()}",
        "auto_create_schema": True,
        "start_background_worker": False,
        "frontend_dist": tmp_path / "missing-dist",
        "_env_file": None,
    }
    values.update(setting_overrides)
    app = create_app(Settings(**values))
    with TestClient(app, raise_server_exceptions=False) as client:
        with app.state.database.engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            )
            connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES ('20260714_0007')")
            )
        yield client


class ApiDoubleFailureFileOperations:
    def __init__(self, real) -> None:
        self.real = real
        self.install_failed = False
        self.rollback_failed = False

    def replace(self, source: Path, destination: Path) -> None:
        if (
            not self.install_failed
            and ".restore-staging" in source.parts
            and source.name == "app.db"
        ):
            self.install_failed = True
            raise OSError("synthetic API install failure")
        if (
            not self.rollback_failed
            and ".restore-rollback" in source.parts
            and source.name == "config"
        ):
            self.rollback_failed = True
            raise OSError("synthetic API config rollback failure")
        self.real.replace(source, destination)

    def remove(self, path: Path) -> None:
        self.real.remove(path)


class PausingRestoreFileOperations:
    def __init__(self, real: FileOperations, phase: str) -> None:
        self.real = real
        self.phase = phase
        self.reached = threading.Event()
        self.release = threading.Event()

    def replace(self, source: Path, destination: Path) -> None:
        moving_live_database = (
            source.name == "app.db" and ".restore-rollback" in destination.parts
        )
        installing_database = (
            source.name == "app.db" and ".restore-staging" in source.parts
        )
        restoring_config = (
            source.name == "config" and ".restore-rollback" in source.parts
        )
        if self.phase == "rollback" and installing_database:
            raise OSError("synthetic install failure before rollback barrier")
        self.real.replace(source, destination)
        if (
            self.phase == "movement" and moving_live_database
        ) or (
            self.phase == "rollback" and restoring_config
        ):
            self.reached.set()
            if not self.release.wait(timeout=10):
                raise TimeoutError("restore barrier was not released")

    def remove(self, path: Path) -> None:
        self.real.remove(path)


def validated_restore_payload(client: TestClient) -> dict[str, object]:
    created = client.post("/api/backups").json()
    validated = client.post(
        "/api/backups/validate", data={"backup_id": created["backup_id"]}
    ).json()
    return {
        "validation_token": validated["validation_token"],
        "confirmed": True,
    }


def test_restore_staging_preserves_uuid_uniqueness_below_windows_legacy_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifiers = iter(
        [
            UUID("11111111-1111-1111-1111-111111111111"),
            UUID("11111111-1111-1111-1111-111111111112"),
        ]
    )
    monkeypatch.setattr(
        backup_manager_module,
        "uuid4",
        lambda: next(identifiers),
    )
    manager = BackupManager.__new__(BackupManager)
    data_dir = PureWindowsPath("C:/") / ("d" * 112)
    manager.staging_root = data_dir / ".restore-staging"

    staging_roots = [manager._next_staging_dir() for _ in range(2)]
    destinations = [
        root
        / "payload"
        / "imports"
        / ("a" * 36)
        / f"{'b' * 36}.png"
        for root in staging_roots
    ]

    assert staging_roots[0] != staging_roots[1]
    assert max(len(str(destination)) for destination in destinations) < 260


async def paused_file_response(
    application,
    path: str,
    response_started: threading.Event,
    release_response: threading.Event,
) -> tuple[int, bytes]:
    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)
        if message["type"] == "http.response.start":
            response_started.set()
            released = await asyncio.to_thread(
                release_response.wait, 10
            )
            if not released:
                raise TimeoutError("file response barrier was not released")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }
    await application(scope, receive, send)
    status = next(
        message["status"]
        for message in messages
        if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return status, body


def test_backup_api_create_list_and_download(tmp_path: Path) -> None:
    with backup_client(tmp_path) as client:
        created = client.post("/api/backups")
        listed = client.get("/api/backups")
        backup_id = created.json()["backup_id"]
        downloaded = client.get(f"/api/backups/{backup_id}/content")

    assert created.status_code == 201
    assert listed.status_code == 200
    assert [item["backup_id"] for item in listed.json()["items"]] == [backup_id]
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "application/zip"
    assert downloaded.content.startswith(b"PK")


def test_backup_creation_semantic_failure_is_safe_operation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_path = str((tmp_path / "private" / "source.png").resolve())

    def reject_staged_payload(self, *args, **kwargs) -> bool:
        raise BackupValidationError(
            f"来源截图与数据库记录不一致: {private_path}"
        )

    monkeypatch.setattr(
        ArchiveValidator,
        "validate_staged_payload",
        reject_staged_payload,
    )

    with backup_client(tmp_path) as client:
        failed = client.post("/api/backups")
        listed = client.get("/api/backups")

    assert failed.status_code == 500
    assert failed.json() == {
        "code": "BACKUP_OPERATION_FAILED",
        "detail": (
            "备份创建期间检测到数据不一致，未生成备份；"
            "请关闭可能修改数据目录的程序后重试"
        ),
    }
    assert private_path not in failed.text
    assert "来源截图与数据库记录不一致" not in failed.text
    assert "Traceback" not in failed.text
    assert listed.status_code == 200
    assert listed.json()["items"] == []


def test_backup_creation_runtime_failure_uses_generic_safe_operation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_path = str((tmp_path / "private" / "source.png").resolve())

    def fail_staged_payload(self, *args, **kwargs) -> bool:
        raise RuntimeError(f"unexpected validation failure at {private_path}")

    monkeypatch.setattr(
        ArchiveValidator,
        "validate_staged_payload",
        fail_staged_payload,
    )

    with backup_client(tmp_path) as client:
        failed = client.post("/api/backups")
        listed = client.get("/api/backups")

    assert failed.status_code == 500
    assert failed.json() == {
        "code": "BACKUP_OPERATION_FAILED",
        "detail": "备份操作失败，请查看本地日志",
    }
    assert private_path not in failed.text
    assert "unexpected validation failure" not in failed.text
    assert "Traceback" not in failed.text
    assert listed.status_code == 200
    assert listed.json()["items"] == []


def test_backup_api_validates_existing_and_uploaded_archive(tmp_path: Path) -> None:
    with backup_client(tmp_path) as client:
        created = client.post("/api/backups").json()
        archive = client.get(
            f"/api/backups/{created['backup_id']}/content"
        ).content
        existing = client.post(
            "/api/backups/validate", data={"backup_id": created["backup_id"]}
        )
        uploaded = client.post(
            "/api/backups/validate",
            files={"file": ("external.zip", archive, "application/zip")},
        )

    assert existing.status_code == 200
    assert uploaded.status_code == 200
    assert existing.json()["hashes_verified"] is True
    assert uploaded.json()["hashes_verified"] is True
    assert len(existing.json()["validation_token"]) >= 32
    assert len(uploaded.json()["validation_token"]) >= 32


def test_backup_api_rejects_uploaded_manifest_count_mismatch(
    tmp_path: Path,
) -> None:
    forged = tmp_path / "forged-counts.zip"
    with backup_client(tmp_path) as client:
        created = client.post("/api/backups").json()
        archive = tmp_path / "data" / "backups" / created["filename"]
        with ZipFile(archive) as source:
            entries = [
                (info, source.read(info.filename))
                for info in source.infolist()
            ]
        with ZipFile(forged, "w") as target:
            for info, payload in entries:
                if info.filename == "manifest.json":
                    manifest = json.loads(payload)
                    manifest["counts"]["questions"] = 999
                    payload = json.dumps(
                        manifest,
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode("utf-8")
                target.writestr(info, payload)

        response = client.post(
            "/api/backups/validate",
            files={
                "file": (
                    "external-forged-counts.zip",
                    forged.read_bytes(),
                    "application/zip",
                )
            },
        )

    assert response.status_code == 400
    assert response.json() == {
        "code": "BACKUP_INVALID",
        "detail": "备份统计数量与数据库记录不一致",
    }
    assert "validation_token" not in response.json()
    for root_name in (".restore-staging", ".restore-uploads"):
        root = tmp_path / "data" / root_name
        assert not root.exists() or not any(root.iterdir())
    assert str(tmp_path.resolve()) not in response.text
    assert "999" not in response.text
    assert "Traceback" not in response.text


def test_backup_validation_requires_exactly_one_source_and_safe_errors(
    tmp_path: Path,
) -> None:
    with backup_client(tmp_path) as client:
        created = client.post("/api/backups").json()
        archive = client.get(
            f"/api/backups/{created['backup_id']}/content"
        ).content
        missing = client.post("/api/backups/validate")
        both = client.post(
            "/api/backups/validate",
            data={"backup_id": created["backup_id"]},
            files={"file": ("external.zip", archive, "application/zip")},
        )
        unknown = client.post(
            "/api/backups/validate",
            data={"backup_id": "00000000-0000-0000-0000-000000000000"},
        )
        corrupt = client.post(
            "/api/backups/validate",
            files={"file": ("broken.zip", b"not-a-zip", "application/zip")},
        )

    assert missing.status_code == 422
    assert both.status_code == 422
    assert unknown.status_code == 404
    assert corrupt.status_code == 400
    for response in (unknown, corrupt):
        body = response.json()
        assert body["code"]
        assert str(tmp_path.resolve()) not in response.text
        assert "Traceback" not in response.text


def test_backup_creation_and_import_upload_refuse_exclusive_conflicts(
    tmp_path: Path,
) -> None:
    with backup_client(tmp_path) as client:
        app = client.app
        with app.state.maintenance.activity("OCR 处理"):
            busy_backup = client.post("/api/backups")
        job = client.post(
            "/api/import-jobs",
            json={
                "course_name": "阶段九",
                "category_path": "维护锁",
                "batch_name": "冲突测试",
                "source_relative_path": "阶段九/维护锁/冲突测试",
            },
        ).json()
        with app.state.maintenance.exclusive("恢复备份"):
            busy_upload = client.post(
                f"/api/import-jobs/{job['id']}/files",
                files=[
                    ("files", ("one.png", b"not-read", "image/png")),
                    ("relative_paths", (None, "one.png")),
                ],
            )

    assert busy_backup.status_code == 409
    assert busy_backup.json()["code"] == "BACKUP_BUSY"
    assert busy_upload.status_code == 409
    assert busy_upload.json()["code"] == "BACKUP_BUSY"


def test_active_business_request_prevents_restore_from_starting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_entered = threading.Event()
    release_request = threading.Event()
    real_list_metadata = library_api.list_library_metadata

    def blocking_list_metadata(*args, **kwargs):
        request_entered.set()
        if not release_request.wait(timeout=10):
            raise TimeoutError("business request barrier was not released")
        return real_list_metadata(*args, **kwargs)

    monkeypatch.setattr(
        library_api, "list_library_metadata", blocking_list_metadata
    )
    with backup_client(tmp_path) as client:
        restore_payload = validated_restore_payload(client)
        with ThreadPoolExecutor(max_workers=2) as pool:
            business_future = pool.submit(client.get, "/api/courses")
            assert request_entered.wait(timeout=10)
            try:
                restore = client.post(
                    "/api/backups/restore", json=restore_payload
                )
            finally:
                release_request.set()
            business = business_future.result(timeout=10)

    assert restore.status_code == 409
    assert restore.json()["code"] == "BACKUP_BUSY"
    assert business.status_code == 200


def test_file_response_holds_business_activity_until_body_completes(
    tmp_path: Path,
) -> None:
    response_started = threading.Event()
    release_response = threading.Event()
    content = b"managed-file-response" * 4096

    with backup_client(tmp_path) as client:
        managed_file = (
            client.app.state.settings.data_dir / "imports" / "managed.bin"
        )
        managed_file.parent.mkdir(parents=True)
        managed_file.write_bytes(content)

        @client.app.get("/api/test-managed-file", response_class=FileResponse)
        def managed_file_response() -> FileResponse:
            return FileResponse(managed_file)

        with ThreadPoolExecutor(max_workers=1) as pool:
            response_future = pool.submit(
                lambda: asyncio.run(
                    paused_file_response(
                        client.app,
                        "/api/test-managed-file",
                        response_started,
                        release_response,
                    )
                )
            )
            assert response_started.wait(timeout=10)
            try:
                with pytest.raises(BackupBusyError):
                    with client.app.state.maintenance.exclusive(
                        "restore backup"
                    ):
                        pass
            finally:
                release_response.set()
            status, body = response_future.result(timeout=10)

    assert status == 200
    assert body == content


@pytest.mark.parametrize("phase", ["movement", "rollback"])
def test_restore_exclusive_rejects_business_entry_during_destructive_work(
    tmp_path: Path,
    phase: str,
) -> None:
    with backup_client(tmp_path) as client:
        config = (
            client.app.state.settings.data_dir
            / "config"
            / "migratable-settings.json"
        )
        config.parent.mkdir(parents=True)
        config.write_text("{}", encoding="utf-8")
        restore_payload = validated_restore_payload(client)
        filesystem = PausingRestoreFileOperations(FileOperations(), phase)
        client.app.state.backup_manager.restorer.filesystem = filesystem

        with ThreadPoolExecutor(max_workers=2) as pool:
            restore_future = pool.submit(
                client.post, "/api/backups/restore", json=restore_payload
            )
            assert filesystem.reached.wait(timeout=10)
            try:
                business = client.get("/api/courses")
                health = client.get("/api/health")
            finally:
                filesystem.release.set()
            restored = restore_future.result(timeout=10)

    assert business.status_code == 409
    assert business.json()["code"] == "BACKUP_BUSY"
    assert health.status_code == 200
    if phase == "movement":
        assert restored.status_code == 200
    else:
        assert restored.status_code == 500
        assert restored.json()["code"] == "BACKUP_RESTORE_FAILED"


def test_restore_requires_confirmation_valid_token_and_seals_app_until_restart(
    tmp_path: Path,
) -> None:
    with backup_client(tmp_path) as client:
        created = client.post("/api/backups").json()
        validated = client.post(
            "/api/backups/validate", data={"backup_id": created["backup_id"]}
        ).json()
        denied = client.post(
            "/api/backups/restore",
            json={
                "validation_token": validated["validation_token"],
                "confirmed": False,
            },
        )
        invalid = client.post(
            "/api/backups/restore",
            json={"validation_token": "x" * 32, "confirmed": True},
        )
        restored = client.post(
            "/api/backups/restore",
            json={
                "validation_token": validated["validation_token"],
                "confirmed": True,
            },
        )
        blocked = client.get("/api/courses")
        health = client.get("/api/health")

    assert denied.status_code == 422
    assert invalid.status_code == 410
    assert restored.status_code == 200
    assert restored.json()["restart_required"] is True
    assert restored.json()["safety_backup"]["reason"] == "pre_restore"
    assert blocked.status_code == 503
    assert blocked.json()["code"] == "RESTART_REQUIRED"
    assert health.status_code == 200


def test_incomplete_restore_immediately_gates_business_apis(
    tmp_path: Path,
) -> None:
    with backup_client(tmp_path) as client:
        config = (
            client.app.state.settings.data_dir
            / "config"
            / "migratable-settings.json"
        )
        config.parent.mkdir(parents=True)
        config.write_text("{}", encoding="utf-8")
        created = client.post("/api/backups").json()
        validated = client.post(
            "/api/backups/validate", data={"backup_id": created["backup_id"]}
        ).json()
        client.app.state.backup_manager.restorer.filesystem = (
            ApiDoubleFailureFileOperations(FileOperations())
        )

        failed = client.post(
            "/api/backups/restore",
            json={
                "validation_token": validated["validation_token"],
                "confirmed": True,
            },
        )
        blocked = client.get("/api/courses")
        health = client.get("/api/health")

    assert failed.status_code == 500
    assert failed.json()["code"] == "BACKUP_RECOVERY_REQUIRED"
    assert "原有数据已保留" not in failed.json()["detail"]
    assert blocked.status_code == 503
    assert blocked.json()["code"] == "BACKUP_RECOVERY_REQUIRED"
    assert health.status_code == 200
    assert health.json()["status"] == "recovery_required"


def test_external_backup_upload_limit_is_streamed_and_enforced(tmp_path: Path) -> None:
    with backup_client(tmp_path, max_backup_upload_bytes=32) as client:
        response = client.post(
            "/api/backups/validate",
            files={"file": ("large.zip", b"x" * 33, "application/zip")},
        )

    assert response.status_code == 400
    assert "大小" in response.json()["detail"]
