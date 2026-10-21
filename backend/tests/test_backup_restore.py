from __future__ import annotations

import inspect
import sqlite3
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.config import Settings
from app.database import Database
from app.models import Category, Course, ImportBatch, SourceImage
from app.services.backups.archive import BackupArchiveService
from app.services.backups.errors import (
    BackupBusyError,
    BackupRecoveryRequiredError,
    BackupRestoreError,
    BackupValidationError,
)
from app.services.backups.recovery import RestoreRecoverySentinel
from app.services.backups.validation import ArchiveValidator, ValidatedBackup


def restore_types():
    from app.services.backups.maintenance import MaintenanceCoordinator
    from app.services.backups.restore import FileOperations, RestoreCoordinator

    return MaintenanceCoordinator, FileOperations, RestoreCoordinator


class FakeWorker:
    def __init__(self, stop_error: Exception | None = None) -> None:
        self.stop_error = stop_error
        self.stop_calls = 0
        self.start_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1
        if self.stop_error:
            raise self.stop_error

    def start(self) -> None:
        self.start_calls += 1


class FailingFileOperations:
    def __init__(self, real, fail_on_call: int) -> None:
        self.real = real
        self.fail_on_call = fail_on_call
        self.calls = 0

    def replace(self, source: Path, destination: Path) -> None:
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise OSError("injected move failure")
        self.real.replace(source, destination)

    def remove(self, path: Path) -> None:
        self.real.remove(path)


class MultiFailingFileOperations:
    def __init__(self, real, fail_on_calls: set[int]) -> None:
        self.real = real
        self.fail_on_calls = fail_on_calls
        self.calls = 0
        self.replace_attempts: list[tuple[Path, Path]] = []

    def replace(self, source: Path, destination: Path) -> None:
        self.calls += 1
        self.replace_attempts.append((source, destination))
        if self.calls in self.fail_on_calls:
            raise OSError(f"injected replace failure {self.calls}")
        self.real.replace(source, destination)

    def remove(self, path: Path) -> None:
        self.real.remove(path)


class MissingRollbackMaterialFileOperations:
    def __init__(self, real) -> None:
        self.real = real
        self.rollback_destinations: list[Path] = []
        self.retained_material: Path | None = None

    def replace(self, source: Path, destination: Path) -> None:
        if ".restore-rollback" in destination.parts:
            self.real.replace(source, destination)
            self.rollback_destinations.append(destination)
            return
        if ".restore-staging" in source.parts:
            missing = self.rollback_destinations[-1]
            self.real.remove(missing)
            self.retained_material = missing.parent / "retained-material.txt"
            self.retained_material.write_text("retain", encoding="utf-8")
            raise OSError("injected install failure after rollback loss")
        self.real.replace(source, destination)

    def remove(self, path: Path) -> None:
        self.real.remove(path)


class CleanupFailingFileOperations:
    def __init__(self, real) -> None:
        self.real = real

    def replace(self, source: Path, destination: Path) -> None:
        self.real.replace(source, destination)

    def remove(self, path: Path) -> None:
        if ".restore-rollback" in path.parts:
            raise OSError("injected rollback cleanup failure")
        self.real.remove(path)


class FailingPostInstallValidator(ArchiveValidator):
    def validate_installed(
        self, database_path: Path, validated: ValidatedBackup
    ) -> None:
        raise BackupValidationError("注入安装后校验失败")


class ArmFailingSentinel(RestoreRecoverySentinel):
    def arm(self, **kwargs):
        raise OSError("synthetic sentinel arm failure")


class ClearFailingSentinel(RestoreRecoverySentinel):
    def clear(self) -> None:
        raise OSError("synthetic sentinel clear failure")


@dataclass
class RestoreEnvironment:
    settings: Settings
    database: Database
    archives: BackupArchiveService
    validator: ArchiveValidator
    validated: ValidatedBackup
    worker: FakeWorker
    database_path: Path
    import_path: Path
    crop_path: Path
    log_path: Path


@pytest.fixture
def restore_env(tmp_path: Path) -> RestoreEnvironment:
    data_dir = tmp_path / "data"
    database_path = data_dir / "app.db"
    settings = Settings(
        data_dir=data_dir,
        database_url=f"sqlite:///{database_path.as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing-dist",
        OCR_ENGINE="mock",
        OCR_DEVICE="cpu",
        _env_file=None,
    )
    database = Database(settings)
    database.create_schema()
    with database.engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('20260714_0007')")
        )

    import_path = data_dir / "imports" / "source.png"
    crop_path = data_dir / "crops" / "question.png"
    log_path = data_dir / "logs" / "app.log"
    import_path.parent.mkdir(parents=True)
    crop_path.parent.mkdir(parents=True)
    log_path.parent.mkdir(parents=True)
    import_path.write_bytes(b"backup-source")
    crop_path.write_bytes(b"backup-crop")
    log_path.write_text("keep-local-log", encoding="utf-8")
    with database.session_factory() as session:
        course = Course(name="备份中的课程")
        category = Category(course=course, path="阶段九")
        batch = ImportBatch(
            course=course,
            category=category,
            name="备份批次",
            source_relative_path="阶段九/备份批次",
            status="ready",
        )
        session.add(
            SourceImage(
                batch=batch,
                source_relative_path="source.png",
                original_name="source.png",
                safe_name="source.png",
                relative_path="imports/source.png",
                sha256=sha256(import_path.read_bytes()).hexdigest(),
                mime_type="image/png",
                width=10,
                height=10,
                file_size=import_path.stat().st_size,
                order_index=0,
                ocr_status="completed",
                ocr_result_json={"source": "backup"},
            )
        )
        session.commit()

    archives = BackupArchiveService(database, settings)
    manual = archives.create()
    archive_path = data_dir / "backups" / manual.filename
    validator = ArchiveValidator(settings)
    validated = validator.validate(archive_path, data_dir / ".restore-staging" / "token")

    with database.session_factory() as session:
        course = session.scalar(select(Course))
        assert course is not None
        course.name = "恢复前当前课程"
        session.commit()
    import_path.write_bytes(b"current-source")
    with database.session_factory() as session:
        source = session.scalar(select(SourceImage))
        assert source is not None
        source.sha256 = sha256(import_path.read_bytes()).hexdigest()
        source.file_size = import_path.stat().st_size
        session.commit()
    crop_path.write_bytes(b"current-crop")
    (data_dir / "ocr").mkdir()
    (data_dir / "ocr" / "current.json").write_text("current", encoding="utf-8")
    (data_dir / "config").mkdir()
    (data_dir / "config" / "migratable-settings.json").write_text(
        '{"ocr_device":"gpu"}', encoding="utf-8"
    )

    yield RestoreEnvironment(
        settings=settings,
        database=database,
        archives=archives,
        validator=validator,
        validated=validated,
        worker=FakeWorker(),
        database_path=database_path,
        import_path=import_path,
        crop_path=crop_path,
        log_path=log_path,
    )
    database.dispose()


def current_state(env: RestoreEnvironment) -> tuple[str, bytes, bytes, str]:
    with sqlite3.connect(env.database_path) as connection:
        course = connection.execute("SELECT name FROM courses").fetchone()[0]
    return (
        course,
        env.import_path.read_bytes(),
        env.crop_path.read_bytes(),
        env.log_path.read_text(encoding="utf-8"),
    )


def coordinator(env: RestoreEnvironment, **overrides):
    MaintenanceCoordinator, FileOperations, RestoreCoordinator = restore_types()
    restart = overrides.pop("restart", [])
    recovery = overrides.pop("recovery", [])
    recovery_sentinel = overrides.pop(
        "recovery_sentinel",
        RestoreRecoverySentinel(env.settings.data_dir),
    )
    return RestoreCoordinator(
        database=env.database,
        settings=env.settings,
        worker=overrides.pop("worker", env.worker),
        archives=overrides.pop("archives", env.archives),
        validator=overrides.pop("validator", env.validator),
        filesystem=overrides.pop("filesystem", FileOperations()),
        recovery_sentinel=recovery_sentinel,
        mark_restart_required=lambda: restart.append(True),
        mark_recovery_required=recovery.append,
        **overrides,
    ), restart


def test_maintenance_refuses_exclusive_work_during_activity_and_inverse() -> None:
    MaintenanceCoordinator, _, _ = restore_types()
    maintenance = MaintenanceCoordinator()

    with maintenance.activity("OCR 处理"):
        with pytest.raises(BackupBusyError, match="OCR 处理"):
            with maintenance.exclusive("恢复备份"):
                pass
    with maintenance.exclusive("创建备份"):
        with pytest.raises(BackupBusyError, match="创建备份"):
            with maintenance.activity("导入图片"):
                pass


@pytest.mark.parametrize(
    "omitted", ["recovery_sentinel", "mark_recovery_required"]
)
def test_restore_coordinator_rejects_omitted_recovery_wiring(
    restore_env: RestoreEnvironment,
    omitted: str,
) -> None:
    _, FileOperations, RestoreCoordinator = restore_types()
    arguments = {
        "database": restore_env.database,
        "settings": restore_env.settings,
        "worker": restore_env.worker,
        "archives": restore_env.archives,
        "validator": restore_env.validator,
        "filesystem": FileOperations(),
        "mark_restart_required": lambda: None,
        "recovery_sentinel": RestoreRecoverySentinel(
            restore_env.settings.data_dir
        ),
        "mark_recovery_required": lambda _: None,
    }
    arguments.pop(omitted)

    with pytest.raises(TypeError):
        RestoreCoordinator(**arguments)


def test_backup_manager_recovery_wiring_is_required_by_signature() -> None:
    from app.services.backups.manager import BackupManager

    parameters = inspect.signature(BackupManager).parameters

    assert parameters["recovery_sentinel"].default is inspect.Parameter.empty
    assert (
        parameters["mark_recovery_required"].default
        is inspect.Parameter.empty
    )


def test_restore_revalidates_staging_before_creating_safety_backup(
    restore_env: RestoreEnvironment,
) -> None:
    config = (
        restore_env.validated.staging_dir
        / "payload"
        / "config"
        / "migratable-settings.json"
    )
    config.write_text("{}", encoding="utf-8")
    service, _ = coordinator(restore_env)

    with pytest.raises(BackupValidationError):
        service.restore(restore_env.validated)

    assert [item.reason for item in restore_env.archives.list()] == ["manual"]
    assert current_state(restore_env) == (
        "恢复前当前课程",
        b"current-source",
        b"current-crop",
        "keep-local-log",
    )


def test_successful_restore_replaces_managed_data_keeps_logs_and_safety_backup(
    restore_env: RestoreEnvironment,
) -> None:
    service, restart = coordinator(restore_env)

    result = service.restore(restore_env.validated)

    assert result.restart_required is True
    assert restart == [True]
    assert result.safety_backup.reason == "pre_restore"
    assert current_state(restore_env) == (
        "备份中的课程",
        b"backup-source",
        b"backup-crop",
        "keep-local-log",
    )
    assert restore_env.worker.stop_calls == 1
    assert not restore_env.validated.staging_dir.exists()
    assert {item.reason for item in restore_env.archives.list()} == {
        "manual",
        "pre_restore",
    }
    assert service.recovery_sentinel.load() is None
    assert not (restore_env.settings.data_dir / ".restore-rollback").exists()


def test_restore_cleanup_failure_still_seals_success_and_keeps_safety_backup(
    restore_env: RestoreEnvironment,
) -> None:
    _, FileOperations, _ = restore_types()
    filesystem = CleanupFailingFileOperations(FileOperations())
    service, restart = coordinator(restore_env, filesystem=filesystem)

    result = service.restore(restore_env.validated)

    assert result.restart_required is True
    assert result.safety_backup.reason == "pre_restore"
    assert restart == [True]
    assert current_state(restore_env) == (
        "备份中的课程",
        b"backup-source",
        b"backup-crop",
        "keep-local-log",
    )
    assert restore_env.worker.stop_calls == 1
    assert restore_env.worker.start_calls == 0
    assert not restore_env.validated.staging_dir.exists()
    rollback_root = restore_env.settings.data_dir / ".restore-rollback"
    assert rollback_root.exists()
    assert list(rollback_root.rglob(".committed"))


def test_backup_manager_startup_cleans_committed_restore_rollback_residue(
    restore_env: RestoreEnvironment,
) -> None:
    from app.services.backups.manager import BackupManager

    MaintenanceCoordinator, _, _ = restore_types()
    rollback_root = restore_env.settings.data_dir / ".restore-rollback"
    (rollback_root / "completed-operation").mkdir(parents=True)
    (rollback_root / "completed-operation" / "old-file").write_text(
        "old", encoding="utf-8"
    )
    (rollback_root / "completed-operation" / ".committed").touch()
    (rollback_root / "interrupted-operation").mkdir()
    (rollback_root / "interrupted-operation" / "old-file").write_text(
        "must-keep", encoding="utf-8"
    )
    manager = BackupManager(
        database=restore_env.database,
        settings=restore_env.settings,
        worker=restore_env.worker,
        maintenance=MaintenanceCoordinator(),
        mark_restart_required=lambda: None,
        recovery_sentinel=RestoreRecoverySentinel(
            restore_env.settings.data_dir
        ),
        mark_recovery_required=lambda _: None,
    )

    manager.cleanup_startup()

    assert not (rollback_root / "completed-operation").exists()
    assert (rollback_root / "interrupted-operation" / "old-file").read_text(
        encoding="utf-8"
    ) == "must-keep"


@pytest.mark.parametrize("fail_on_call", [1, 2, 5, 6, 8, 10])
def test_restore_move_failures_preserve_current_database_images_and_logs(
    restore_env: RestoreEnvironment, fail_on_call: int
) -> None:
    _, FileOperations, _ = restore_types()
    filesystem = FailingFileOperations(FileOperations(), fail_on_call)
    service, restart = coordinator(restore_env, filesystem=filesystem)
    expected = current_state(restore_env)

    with pytest.raises(BackupRestoreError):
        service.restore(restore_env.validated)

    assert current_state(restore_env) == expected
    assert restart == []


def test_incomplete_rollback_requires_manual_recovery_and_keeps_materials(
    restore_env: RestoreEnvironment,
) -> None:
    _, FileOperations, _ = restore_types()
    filesystem = MultiFailingFileOperations(FileOperations(), {6, 7})
    recovery = []
    service, restart = coordinator(
        restore_env,
        filesystem=filesystem,
        recovery=recovery,
    )
    config = (
        restore_env.settings.data_dir
        / "config"
        / "migratable-settings.json"
    )

    with pytest.raises(BackupRecoveryRequiredError) as exc_info:
        service.restore(restore_env.validated)

    assert exc_info.value.code == "BACKUP_RECOVERY_REQUIRED"
    assert "原有数据已保留" not in exc_info.value.public_message
    assert (
        str(restore_env.settings.data_dir.resolve())
        not in exc_info.value.public_message
    )
    assert recovery == [service.recovery_sentinel.load()]
    assert restart == []
    assert restore_env.worker.start_calls == 0
    assert not config.exists()
    assert list(
        (restore_env.settings.data_dir / ".restore-rollback").rglob(
            "migratable-settings.json"
        )
    )
    safety = [
        item
        for item in restore_env.archives.list()
        if item.reason == "pre_restore"
    ]
    assert len(safety) == 1
    assert safety[0].filename in exc_info.value.public_message
    assert filesystem.calls == 11


def test_missing_moved_live_material_requires_manual_recovery(
    restore_env: RestoreEnvironment,
) -> None:
    _, FileOperations, _ = restore_types()
    restore_env.settings.start_background_worker = True
    filesystem = MissingRollbackMaterialFileOperations(FileOperations())
    recovery = []
    service, restart = coordinator(
        restore_env,
        filesystem=filesystem,
        recovery=recovery,
    )

    with pytest.raises(BackupRecoveryRequiredError) as exc_info:
        service.restore(restore_env.validated)

    assert exc_info.value.code == "BACKUP_RECOVERY_REQUIRED"
    assert "原有数据已保留" not in exc_info.value.public_message
    recovery_state = service.recovery_sentinel.load()
    assert recovery_state is not None
    assert recovery == [recovery_state]
    assert service.recovery_sentinel.path.exists()
    assert restore_env.worker.start_calls == 0
    assert restart == []
    assert filesystem.retained_material is not None
    assert filesystem.retained_material.read_text(encoding="utf-8") == "retain"
    config = (
        restore_env.settings.data_dir
        / "config"
        / "migratable-settings.json"
    )
    assert not config.exists()
    safety = [
        item
        for item in restore_env.archives.list()
        if item.reason == "pre_restore"
    ]
    assert len(safety) == 1
    assert (
        restore_env.settings.data_dir / "backups" / safety[0].filename
    ).exists()
    assert safety[0].filename in exc_info.value.public_message
    assert (
        str(restore_env.settings.data_dir.resolve())
        not in exc_info.value.public_message
    )


def test_sentinel_arm_failure_happens_before_destructive_restore_steps(
    restore_env: RestoreEnvironment,
) -> None:
    _, FileOperations, _ = restore_types()
    filesystem = MultiFailingFileOperations(FileOperations(), set())
    sentinel = ArmFailingSentinel(restore_env.settings.data_dir)
    expected = current_state(restore_env)
    recovery = []
    service, restart = coordinator(
        restore_env,
        filesystem=filesystem,
        recovery_sentinel=sentinel,
        recovery=recovery,
    )

    with pytest.raises(BackupRestoreError, match="无法安全启动"):
        service.restore(restore_env.validated)

    assert filesystem.calls == 0
    assert restore_env.worker.stop_calls == 0
    assert current_state(restore_env) == expected
    assert restart == recovery == []


def test_clear_failure_after_complete_rollback_fails_closed(
    restore_env: RestoreEnvironment,
) -> None:
    _, FileOperations, _ = restore_types()
    filesystem = FailingFileOperations(FileOperations(), fail_on_call=6)
    sentinel = ClearFailingSentinel(restore_env.settings.data_dir)
    recovery = []
    service, restart = coordinator(
        restore_env,
        filesystem=filesystem,
        recovery_sentinel=sentinel,
        recovery=recovery,
    )

    with pytest.raises(BackupRecoveryRequiredError):
        service.restore(restore_env.validated)

    assert recovery
    assert restart == []
    assert restore_env.worker.start_calls == 0
    assert sentinel.path.exists()


def test_clear_failure_after_success_keeps_committed_rollback_materials(
    restore_env: RestoreEnvironment,
) -> None:
    sentinel = ClearFailingSentinel(restore_env.settings.data_dir)
    recovery = []
    service, restart = coordinator(
        restore_env,
        recovery_sentinel=sentinel,
        recovery=recovery,
    )

    with pytest.raises(BackupRecoveryRequiredError):
        service.restore(restore_env.validated)

    rollback_root = restore_env.settings.data_dir / ".restore-rollback"
    assert list(rollback_root.rglob(".committed"))
    assert list(rollback_root.rglob("migratable-settings.json"))
    assert recovery
    assert restart == [True]
    assert sentinel.path.exists()


def test_complete_rollback_clears_sentinel_before_worker_restart(
    restore_env: RestoreEnvironment,
) -> None:
    _, FileOperations, _ = restore_types()
    restore_env.settings.start_background_worker = True
    filesystem = FailingFileOperations(FileOperations(), fail_on_call=6)
    recovery = []
    service, restart = coordinator(
        restore_env,
        filesystem=filesystem,
        recovery=recovery,
    )

    with pytest.raises(BackupRestoreError):
        service.restore(restore_env.validated)

    assert service.recovery_sentinel.load() is None
    assert restore_env.worker.start_calls == 1
    assert recovery == []
    assert restart == []


def test_restore_stop_dispose_and_post_install_failures_preserve_current_data(
    restore_env: RestoreEnvironment, monkeypatch
) -> None:
    expected = current_state(restore_env)
    restore_env.settings.start_background_worker = True
    stopped, _ = coordinator(
        restore_env, worker=FakeWorker(OSError("worker stop failed"))
    )
    with pytest.raises(BackupRestoreError):
        stopped.restore(restore_env.validated)
    assert current_state(restore_env) == expected
    assert stopped.worker.stop_calls == 1
    assert stopped.worker.start_calls == 0

    restore_env.settings.start_background_worker = False

    monkeypatch.setattr(restore_env.database, "dispose", lambda: (_ for _ in ()).throw(OSError("dispose failed")))
    disposed, _ = coordinator(restore_env)
    with pytest.raises(BackupRestoreError):
        disposed.restore(restore_env.validated)
    assert current_state(restore_env) == expected
    monkeypatch.undo()

    post_check, _ = coordinator(
        restore_env,
        validator=FailingPostInstallValidator(restore_env.settings),
    )
    with pytest.raises(BackupRestoreError):
        post_check.restore(restore_env.validated)
    assert current_state(restore_env) == expected
