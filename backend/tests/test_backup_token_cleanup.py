from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import logging
from pathlib import Path
import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
import app.services.backups.tokens as tokens_module
from app.config import Settings
from app.main import create_app
from app.schemas.backups import (
    BackupCounts,
    BackupFileEntry,
    BackupManifest,
)
from app.services.backups.errors import BackupTokenExpiredError
from app.services.backups.recovery import RestoreRecoverySentinel
from app.services.backups.tokens import RestoreTokenRegistry
from app.services.backups.validation import ValidatedBackup


def local_settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    return Settings(
        data_dir=data_dir,
        database_url=f"sqlite:///{(data_dir / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing-dist",
        _env_file=None,
    )


def validated_backup(staging_dir: Path) -> ValidatedBackup:
    staging_dir.mkdir(parents=True)
    return ValidatedBackup(
        manifest=BackupManifest(
            backup_id=uuid4(),
            app_version="0.1.0",
            schema_revision="20260714_0007",
            created_at=datetime.now(timezone.utc),
            file_count=1,
            uncompressed_bytes=1,
            counts=BackupCounts(),
            files=[
                BackupFileEntry(
                    path="payload/app.db",
                    kind="database",
                    size=1,
                    sha256="a" * 64,
                )
            ],
        ),
        staging_dir=staging_dir,
        filename="backup.zip",
        migration_required=False,
        warnings=(),
    )


def test_cleanup_worker_removes_expired_staging_without_registry_activity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_time = [100.0]
    registry = RestoreTokenRegistry(
        ttl_seconds=10,
        clock=lambda: current_time[0],
        cleanup_interval_seconds=0.01,
    )
    validated = validated_backup(tmp_path / "expired-staging")
    discarded = threading.Event()
    original_discard = registry.discard_validated

    def observable_discard(item: ValidatedBackup) -> None:
        original_discard(item)
        discarded.set()

    monkeypatch.setattr(registry, "discard_validated", observable_discard)
    token = registry.issue(validated)
    current_time[0] = 111.0

    registry.start_cleanup()
    try:
        assert discarded.wait(timeout=1)
    finally:
        registry.stop_cleanup()

    assert not validated.staging_dir.exists()
    with registry._lock:
        assert token not in registry._entries
    with pytest.raises(BackupTokenExpiredError):
        registry.consume(token)


def test_cleanup_worker_is_idempotent_and_preserves_unexpired_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_time = [100.0]
    registry = RestoreTokenRegistry(
        ttl_seconds=10,
        clock=lambda: current_time[0],
        cleanup_interval_seconds=0.01,
    )
    validated = validated_backup(tmp_path / "unexpired-staging")
    token = registry.issue(validated)
    cycle_finished = threading.Event()
    original_cleanup = registry.cleanup_expired

    def observable_cleanup() -> None:
        original_cleanup()
        cycle_finished.set()

    monkeypatch.setattr(registry, "cleanup_expired", observable_cleanup)
    registry.start_cleanup()
    first_thread = registry._cleanup_thread
    registry.start_cleanup()
    try:
        assert first_thread is not None
        assert registry._cleanup_thread is first_thread
        assert cycle_finished.wait(timeout=1)
        assert validated.staging_dir.exists()
        assert registry.consume(token) == validated
    finally:
        registry.stop_cleanup()
        registry.stop_cleanup()

    assert not first_thread.is_alive()


def test_cleanup_start_failure_does_not_publish_unstarted_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = RestoreTokenRegistry(ttl_seconds=10)

    def fail_start(_: threading.Thread) -> None:
        raise RuntimeError("synthetic cleanup thread start failure")

    monkeypatch.setattr(threading.Thread, "start", fail_start)

    with pytest.raises(RuntimeError, match="synthetic cleanup thread start failure"):
        registry.start_cleanup()

    assert registry._cleanup_thread is None
    registry.stop_cleanup()


def test_cleanup_stop_timeout_is_logged_and_restart_is_recoverable(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = RestoreTokenRegistry(
        ttl_seconds=10,
        cleanup_interval_seconds=0.01,
    )
    cleanup_entered = threading.Event()
    allow_cleanup_to_finish = threading.Event()

    def blocking_cleanup() -> None:
        cleanup_entered.set()
        assert allow_cleanup_to_finish.wait(timeout=1)

    registry.cleanup_expired = blocking_cleanup
    monkeypatch.setattr(tokens_module, "CLEANUP_JOIN_TIMEOUT_SECONDS", 0.01)
    caplog.set_level(logging.ERROR, logger="app.services.backups.tokens")

    registry.start_cleanup()
    original_thread = registry._cleanup_thread
    assert original_thread is not None
    assert cleanup_entered.wait(timeout=1)

    replacement_thread: threading.Thread | None = None
    try:
        registry.stop_cleanup()
        assert original_thread.is_alive()
        with pytest.raises(RuntimeError, match="still stopping"):
            registry.start_cleanup()

        allow_cleanup_to_finish.set()
        original_thread.join(timeout=1)
        assert not original_thread.is_alive()

        registry.start_cleanup()
        replacement_thread = registry._cleanup_thread
        assert replacement_thread is not None
        assert replacement_thread is not original_thread
        assert replacement_thread.is_alive()
    finally:
        allow_cleanup_to_finish.set()
        registry.stop_cleanup()
        original_thread.join(timeout=1)

    assert replacement_thread is not None
    assert not replacement_thread.is_alive()
    assert "恢复令牌清理线程未能在" in caplog.text


def test_concurrent_start_cannot_cross_an_in_progress_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = RestoreTokenRegistry(
        ttl_seconds=10,
        cleanup_interval_seconds=10.0,
    )
    registry.start_cleanup()
    original_thread = registry._cleanup_thread
    assert original_thread is not None
    original_join = original_thread.join
    join_completed = threading.Event()
    allow_stop_to_finish = threading.Event()

    def controlled_join(timeout: float | None = None) -> None:
        original_join(timeout=timeout)
        join_completed.set()
        assert allow_stop_to_finish.wait(timeout=1)

    monkeypatch.setattr(original_thread, "join", controlled_join)

    replacement_thread: threading.Thread | None = None
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            stopped = executor.submit(registry.stop_cleanup)
            assert join_completed.wait(timeout=1)
            try:
                with pytest.raises(RuntimeError, match="still stopping"):
                    registry.start_cleanup()
            finally:
                allow_stop_to_finish.set()
            stopped.result(timeout=1)

        assert registry._cleanup_thread is None
        registry.start_cleanup()
        replacement_thread = registry._cleanup_thread
        assert replacement_thread is not None
        assert replacement_thread is not original_thread
        assert replacement_thread.is_alive()
    finally:
        allow_stop_to_finish.set()
        registry.stop_cleanup()

    assert replacement_thread is not None
    assert not replacement_thread.is_alive()


def test_cleanup_worker_logs_cycle_failure_and_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = RestoreTokenRegistry(
        ttl_seconds=10,
        cleanup_interval_seconds=0.01,
    )
    recovered = threading.Event()
    calls = 0

    def flaky_cleanup() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic cleanup failure")
        recovered.set()

    caplog.set_level(logging.ERROR, logger="app.services.backups.tokens")
    registry.cleanup_expired = flaky_cleanup
    registry.start_cleanup()
    try:
        assert recovered.wait(timeout=1)
    finally:
        registry.stop_cleanup()

    assert calls >= 2
    assert "过期恢复令牌暂存目录清理失败" in caplog.text


@pytest.mark.parametrize(
    ("ttl_seconds", "expected_interval"),
    [(10, 10.0), (1_800, 60.0)],
)
def test_cleanup_worker_uses_bounded_interval_and_single_daemon_thread(
    ttl_seconds: int,
    expected_interval: float,
) -> None:
    registry = RestoreTokenRegistry(ttl_seconds=ttl_seconds)

    assert registry.cleanup_interval_seconds == expected_interval
    registry.start_cleanup()
    cleanup_thread = registry._cleanup_thread
    registry.start_cleanup()
    try:
        assert cleanup_thread is not None
        assert registry._cleanup_thread is cleanup_thread
        assert cleanup_thread.name == "restore-token-cleanup"
        assert cleanup_thread.daemon is True
    finally:
        registry.stop_cleanup()


def test_stop_cleanup_interrupts_a_long_wait() -> None:
    registry = RestoreTokenRegistry(
        ttl_seconds=10,
        cleanup_interval_seconds=10.0,
    )
    registry.start_cleanup()
    cleanup_thread = registry._cleanup_thread
    assert cleanup_thread is not None

    with ThreadPoolExecutor(max_workers=1) as executor:
        stopped = executor.submit(registry.stop_cleanup)
        stopped.result(timeout=1)

    assert not cleanup_thread.is_alive()
    assert registry._cleanup_thread is None


def test_cleanup_worker_deletes_outside_registry_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_time = [100.0]
    registry = RestoreTokenRegistry(
        ttl_seconds=10,
        clock=lambda: current_time[0],
        cleanup_interval_seconds=0.01,
    )
    expired = validated_backup(tmp_path / "blocking-delete")
    replacement = validated_backup(tmp_path / "replacement")
    registry.issue(expired)
    current_time[0] = 111.0
    deletion_entered = threading.Event()
    allow_deletion = threading.Event()
    original_discard = registry.discard_validated

    def blocking_discard(item: ValidatedBackup) -> None:
        deletion_entered.set()
        assert allow_deletion.wait(timeout=1)
        original_discard(item)

    monkeypatch.setattr(registry, "discard_validated", blocking_discard)
    registry.start_cleanup()
    try:
        assert deletion_entered.wait(timeout=1)
        with ThreadPoolExecutor(max_workers=1) as executor:
            issued = executor.submit(registry.issue, replacement)
            try:
                replacement_token = issued.result(timeout=1)
            finally:
                allow_deletion.set()
        assert registry.consume(replacement_token) == replacement
    finally:
        allow_deletion.set()
        registry.stop_cleanup()

    assert not expired.staging_dir.exists()


def test_cleanup_first_claims_validated_backup_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_time = [100.0]
    registry = RestoreTokenRegistry(ttl_seconds=10, clock=lambda: current_time[0])
    validated = validated_backup(tmp_path / "cleanup-first")
    token = registry.issue(validated)
    current_time[0] = 111.0
    deletion_entered = threading.Event()
    allow_deletion = threading.Event()
    discarded: list[ValidatedBackup] = []
    original_discard = registry.discard_validated

    def blocking_discard(item: ValidatedBackup) -> None:
        discarded.append(item)
        deletion_entered.set()
        assert allow_deletion.wait(timeout=1)
        original_discard(item)

    monkeypatch.setattr(registry, "discard_validated", blocking_discard)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            cleaned = executor.submit(registry.cleanup_expired)
            assert deletion_entered.wait(timeout=1)
            consumed = executor.submit(registry.consume, token)
            try:
                with pytest.raises(BackupTokenExpiredError):
                    consumed.result(timeout=1)
            finally:
                allow_deletion.set()
            cleaned.result(timeout=1)
    finally:
        allow_deletion.set()

    assert discarded == [validated]
    assert not validated.staging_dir.exists()
    with registry._lock:
        assert token not in registry._entries


def test_consume_first_owns_validated_backup_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_clock_entered = threading.Event()
    allow_cleanup_clock = threading.Event()

    def controlled_clock() -> float:
        if threading.current_thread().name.startswith("cleanup-owner"):
            cleanup_clock_entered.set()
            assert allow_cleanup_clock.wait(timeout=1)
            return 111.0
        return 100.0

    registry = RestoreTokenRegistry(ttl_seconds=10, clock=controlled_clock)
    validated = validated_backup(tmp_path / "consume-first")
    token = registry.issue(validated)
    discarded: list[ValidatedBackup] = []
    original_discard = registry.discard_validated

    def observable_discard(item: ValidatedBackup) -> None:
        discarded.append(item)
        original_discard(item)

    monkeypatch.setattr(registry, "discard_validated", observable_discard)

    try:
        with ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="cleanup-owner",
        ) as cleanup_executor:
            cleaned = cleanup_executor.submit(registry.cleanup_expired)
            assert cleanup_clock_entered.wait(timeout=1)
            with ThreadPoolExecutor(max_workers=1) as consume_executor:
                consumed = consume_executor.submit(registry.consume, token)
                assert consumed.result(timeout=1) is validated
            allow_cleanup_clock.set()
            cleaned.result(timeout=1)
    finally:
        allow_cleanup_clock.set()

    assert discarded == []
    assert validated.staging_dir.exists()
    with registry._lock:
        assert token not in registry._entries
    original_discard(validated)


def test_discard_first_claims_validated_backup_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_clock_entered = threading.Event()
    allow_cleanup_clock = threading.Event()

    def controlled_clock() -> float:
        if threading.current_thread().name.startswith("discard-race-cleanup"):
            cleanup_clock_entered.set()
            assert allow_cleanup_clock.wait(timeout=1)
            return 111.0
        return 100.0

    registry = RestoreTokenRegistry(ttl_seconds=10, clock=controlled_clock)
    validated = validated_backup(tmp_path / "discard-first")
    token = registry.issue(validated)
    discarded: list[ValidatedBackup] = []
    original_discard = registry.discard_validated

    def observable_discard(item: ValidatedBackup) -> None:
        discarded.append(item)
        original_discard(item)

    monkeypatch.setattr(registry, "discard_validated", observable_discard)

    try:
        with ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="discard-race-cleanup",
        ) as cleanup_executor:
            cleaned = cleanup_executor.submit(registry.cleanup_expired)
            assert cleanup_clock_entered.wait(timeout=1)
            with ThreadPoolExecutor(max_workers=1) as discard_executor:
                discarded_first = discard_executor.submit(registry.discard, token)
                discarded_first.result(timeout=1)
            allow_cleanup_clock.set()
            cleaned.result(timeout=1)
    finally:
        allow_cleanup_clock.set()

    assert discarded == [validated]
    assert not validated.staging_dir.exists()
    with registry._lock:
        assert token not in registry._entries


def test_cleanup_claims_expired_entry_while_holding_registry_lock(
    tmp_path: Path,
) -> None:
    current_time = [100.0]
    registry = RestoreTokenRegistry(ttl_seconds=10, clock=lambda: current_time[0])
    registry.issue(validated_backup(tmp_path / "lock-owned-entry"))
    current_time[0] = 111.0
    lock_states: list[bool] = []

    class LockObservingEntries(dict):
        def pop(self, key: str, *default: object) -> object:
            lock_states.append(registry._lock.locked())
            return super().pop(key, *default)

    registry._entries = LockObservingEntries(registry._entries)

    registry.cleanup_expired()

    assert lock_states == [True]


def test_app_lifespan_runs_idle_token_cleanup_and_stops_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = local_settings(tmp_path)
    application = create_app(settings)
    current_time = [100.0]
    registry = RestoreTokenRegistry(
        ttl_seconds=10,
        clock=lambda: current_time[0],
        cleanup_interval_seconds=0.01,
    )
    application.state.backup_manager.tokens = registry
    validated = validated_backup(tmp_path / "lifespan-staging")
    discarded = threading.Event()
    original_discard = registry.discard_validated

    def observable_discard(item: ValidatedBackup) -> None:
        original_discard(item)
        discarded.set()

    monkeypatch.setattr(registry, "discard_validated", observable_discard)

    with TestClient(application) as client:
        assert client.get("/api/health").status_code == 200
        cleanup_thread = registry._cleanup_thread
        assert cleanup_thread is not None
        assert cleanup_thread.is_alive()
        token = registry.issue(validated)
        current_time[0] = 111.0
        assert discarded.wait(timeout=1)
        assert not validated.staging_dir.exists()
        with pytest.raises(BackupTokenExpiredError):
            registry.consume(token)

    assert not cleanup_thread.is_alive()
    assert registry._cleanup_thread is None


def test_recovery_locked_lifespan_does_not_start_token_cleanup(
    tmp_path: Path,
) -> None:
    settings = local_settings(tmp_path)
    RestoreRecoverySentinel(settings.data_dir).arm(
        operation_id="operation-20",
        safety_backup_id="backup-20",
        safety_backup_filename="backup-20.zip",
    )
    application = create_app(settings)
    registry = RestoreTokenRegistry(
        ttl_seconds=10,
        cleanup_interval_seconds=0.01,
    )
    application.state.backup_manager.tokens = registry

    with TestClient(application) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "recovery_required"
        assert registry._cleanup_thread is None

    assert registry._cleanup_thread is None


def test_app_teardown_continues_after_token_cleanup_stop_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = create_app(local_settings(tmp_path))
    calls: list[str] = []
    original_worker_stop = application.state.ocr_worker.stop
    original_database_dispose = application.state.database.dispose
    original_close_logging = main_module.close_logging

    monkeypatch.setattr(
        application.state.backup_manager,
        "start_token_cleanup",
        lambda: None,
    )

    def fail_token_cleanup_stop() -> None:
        calls.append("token-cleanup")
        raise RuntimeError("synthetic token cleanup stop failure")

    def observable_worker_stop() -> None:
        calls.append("ocr")
        original_worker_stop()

    def observable_database_dispose() -> None:
        calls.append("database")
        original_database_dispose()

    def observable_close_logging(handler: logging.Handler) -> None:
        calls.append("logging")
        original_close_logging(handler)

    monkeypatch.setattr(
        application.state.backup_manager,
        "stop_token_cleanup",
        fail_token_cleanup_stop,
    )
    monkeypatch.setattr(application.state.ocr_worker, "stop", observable_worker_stop)
    monkeypatch.setattr(
        application.state.database,
        "dispose",
        observable_database_dispose,
    )
    monkeypatch.setattr(main_module, "close_logging", observable_close_logging)

    with pytest.raises(RuntimeError, match="synthetic token cleanup stop failure"):
        with TestClient(application) as client:
            assert client.get("/api/health").status_code == 200

    assert calls == ["token-cleanup", "ocr", "database", "logging"]
