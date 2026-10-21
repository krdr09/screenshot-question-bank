import json
import logging
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import pytest

import app.logging_config as logging_config
from app.logging_config import close_logging, configure_logging


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def restore_root_logger_level() -> Iterator[None]:
    root = logging.getLogger()
    original_level = root.level
    try:
        yield
    finally:
        root.setLevel(original_level)


def _target_handlers(log_path: Path) -> list[TimedRotatingFileHandler]:
    target = log_path.resolve(strict=False)
    return [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, TimedRotatingFileHandler)
        and Path(handler.baseFilename).resolve(strict=False) == target
    ]


def _remove_target_handlers(log_path: Path) -> None:
    root = logging.getLogger()
    for handler in _target_handlers(log_path):
        root.removeHandler(handler)
        handler.close()


def test_configure_logging_owns_and_closes_handler_it_creates(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "logs" / "app.log"
    root = logging.getLogger()
    lifecycle = configure_logging(log_path.parent)
    released = False

    try:
        assert lifecycle.owned is True
        assert lifecycle.handler in root.handlers

        close_logging(lifecycle)
        released = True

        assert lifecycle.handler not in root.handlers
        assert lifecycle.handler.stream is None

        replacement = configure_logging(log_path.parent)
        try:
            assert replacement.handler is not lifecycle.handler
            assert _target_handlers(log_path) == [replacement.handler]
        finally:
            close_logging(replacement)
        assert replacement.handler.stream is None
    finally:
        if not released:
            close_logging(lifecycle)
        if lifecycle.handler.stream is not None:
            lifecycle.handler.close()
        _remove_target_handlers(log_path)


def test_configure_logging_reuses_matching_handler_without_closing_it(
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    external = TimedRotatingFileHandler(
        log_dir / "app.log",
        when="midnight",
        backupCount=30,
        encoding="utf-8",
    )
    root = logging.getLogger()
    root.addHandler(external)

    try:
        lifecycle = configure_logging(log_dir)

        assert lifecycle.handler is external
        assert lifecycle.owned is False
        assert root.handlers.count(external) == 1

        close_logging(lifecycle)

        assert external in root.handlers
        assert external.stream is not None
        assert not external.stream.closed
    finally:
        if external in root.handlers:
            root.removeHandler(external)
        external.close()


def test_module_owned_handler_remains_until_last_overlapping_lease_closes(
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "logs"
    log_path = log_dir / "app.log"
    first = configure_logging(log_dir)
    second = configure_logging(log_dir / ".." / "logs")
    released: set[int] = set()

    try:
        assert first.handler is second.handler
        assert first.owned is True
        assert second.owned is True
        assert _target_handlers(log_path) == [first.handler]

        close_logging(first)
        released.add(id(first))

        assert _target_handlers(log_path) == [second.handler]
        assert second.handler.stream is not None
        logging.getLogger("test.logging.overlap").info("owner-first-overlap-marker")
        second.handler.flush()
        assert log_path.read_text(encoding="utf-8").count(
            "owner-first-overlap-marker"
        ) == 1

        close_logging(second)
        released.add(id(second))

        assert _target_handlers(log_path) == []
        assert second.handler.stream is None
    finally:
        for lifecycle in (first, second):
            if id(lifecycle) not in released:
                close_logging(lifecycle)
        _remove_target_handlers(log_path)


def test_concurrent_configure_installs_one_reference_counted_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_dir = tmp_path / "logs"
    log_path = log_dir / "app.log"
    constructor_entered = threading.Event()
    second_lock_attempted = threading.Event()
    constructor_gate = threading.Lock()
    lock_attempt_gate = threading.Lock()
    lock_attempts = 0
    created_handlers: list[TimedRotatingFileHandler] = []
    original_handler_class = logging_config.TimedRotatingFileHandler
    shared_lock = logging_config._handler_lock

    class ObservedSharedLock:
        def __enter__(self):
            nonlocal lock_attempts
            with lock_attempt_gate:
                lock_attempts += 1
                if lock_attempts == 2:
                    second_lock_attempted.set()
            shared_lock.acquire()
            return self

        def __exit__(self, *exc_info) -> None:
            shared_lock.release()

    class SynchronizedTimedRotatingFileHandler(original_handler_class):
        def __init__(self, *args, **kwargs) -> None:
            with constructor_gate:
                is_first = not constructor_entered.is_set()
                if is_first:
                    constructor_entered.set()
            if is_first and not second_lock_attempted.wait(timeout=5):
                raise AssertionError("second configure never attempted the shared lock")
            super().__init__(*args, **kwargs)
            created_handlers.append(self)

    monkeypatch.setattr(
        logging_config,
        "TimedRotatingFileHandler",
        SynchronizedTimedRotatingFileHandler,
    )
    monkeypatch.setattr(
        logging_config,
        "_handler_lock",
        ObservedSharedLock(),
    )

    lifecycles = []
    released: set[int] = set()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(configure_logging, log_dir)
            assert constructor_entered.wait(timeout=5)
            second_future = executor.submit(configure_logging, log_dir)
            lifecycles = [
                first_future.result(timeout=10),
                second_future.result(timeout=10),
            ]

        first, second = lifecycles
        assert first.handler is second.handler
        assert first.owned is True
        assert second.owned is True
        assert created_handlers == [first.handler]
        assert _target_handlers(log_path) == [first.handler]

        close_logging(first)
        released.add(id(first))
        assert _target_handlers(log_path) == [second.handler]

        close_logging(second)
        released.add(id(second))
        assert _target_handlers(log_path) == []
        assert second.handler.stream is None
    finally:
        for lifecycle in lifecycles:
            if id(lifecycle) not in released:
                close_logging(lifecycle)
        _remove_target_handlers(log_path)


def test_concurrent_final_release_does_not_close_a_new_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_dir = tmp_path / "logs"
    log_path = log_dir / "app.log"
    first = configure_logging(log_dir)
    shared_lock = logging_config._handler_lock
    original_registry = logging_config._owned_handlers
    close_reached_delete = threading.Event()
    configure_attempted_lock = threading.Event()
    thread_role = threading.local()
    replacement = None

    class ObservedSharedLock:
        def __enter__(self):
            if getattr(thread_role, "value", None) == "configure":
                configure_attempted_lock.set()
            shared_lock.acquire()
            return self

        def __exit__(self, *exc_info) -> None:
            shared_lock.release()

    class BlockingRegistry(dict):
        def __delitem__(self, key) -> None:
            close_reached_delete.set()
            if not configure_attempted_lock.wait(timeout=5):
                raise AssertionError("configure never attempted the shared lock")
            super().__delitem__(key)

    blocking_registry = BlockingRegistry(original_registry)
    original_registry.clear()
    monkeypatch.setattr(logging_config, "_handler_lock", ObservedSharedLock())
    monkeypatch.setattr(logging_config, "_owned_handlers", blocking_registry)

    def release_first() -> None:
        thread_role.value = "release"
        close_logging(first)

    def configure_replacement():
        thread_role.value = "configure"
        return configure_logging(log_dir)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            release_future = executor.submit(release_first)
            assert close_reached_delete.wait(timeout=5)
            configure_future = executor.submit(configure_replacement)
            replacement = configure_future.result(timeout=10)
            release_future.result(timeout=10)

        assert first.handler.stream is None
        assert replacement.handler is not first.handler
        assert replacement.handler.stream is not None
        assert _target_handlers(log_path) == [replacement.handler]
    finally:
        close_logging(first)
        if replacement is not None:
            close_logging(replacement)
        _remove_target_handlers(log_path)
        blocking_registry.clear()


def test_uvicorn_logging_config_uses_custom_data_dir_and_one_root_handler(
    tmp_path: Path,
) -> None:
    launch_root = tmp_path / "launch-root"
    default_log_dir = launch_root / "data" / "logs"
    default_log_dir.mkdir(parents=True)
    custom_data_dir = tmp_path / "custom-data"
    custom_log_path = custom_data_dir / "logs" / "app.log"
    script = r'''
import json
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
import sys

from uvicorn.main import main
from uvicorn.server import Server

sandbox_root = Path(sys.argv[1])
app_dir = Path(sys.argv[2])
config_path = Path(sys.argv[3])
configured_interval = json.loads(
    config_path.read_text(encoding="utf-8")
)["handlers"]["rotating_file"]["interval"]

def inspect_server(self, sockets=None):
    from app.config import Settings
    from app.logging_config import close_logging, configure_logging

    current = Settings()
    log_path = current.data_dir / "logs" / "app.log"
    lifecycle = configure_logging(log_path.parent)
    markers = {
        "uvicorn": "bug-021-uvicorn-marker",
        "uvicorn.error": "bug-021-error-marker",
        "uvicorn.access": "bug-021-access-marker",
    }
    for logger_name, marker in markers.items():
        logging.getLogger(logger_name).info(marker)

    root = logging.getLogger()
    for handler in root.handlers:
        handler.flush()
    contents = log_path.read_text(encoding="utf-8")
    root_rotating_handlers = [
        handler
        for handler in root.handlers
        if isinstance(handler, TimedRotatingFileHandler)
    ]
    payload = {
        "configured_interval": configured_interval,
        "settings_data_dir": str(current.data_dir.resolve()),
        "owned": lifecycle.owned,
        "root_rotating_handlers": len(root_rotating_handlers),
        "root_handler_paths": sorted(
            str(Path(handler.baseFilename).resolve())
            for handler in root_rotating_handlers
        ),
        "when": lifecycle.handler.when,
        "runtime_interval": lifecycle.handler.interval,
        "backup_count": lifecycle.handler.backupCount,
        "encoding": lifecycle.handler.encoding,
        "logger_handlers": {
            name: len(logging.getLogger(name).handlers) for name in markers
        },
        "logger_propagates": {
            name: logging.getLogger(name).propagate for name in markers
        },
        "marker_counts": {
            name: contents.count(marker) for name, marker in markers.items()
        },
        "logging_config_module": str(
            Path(sys.modules["app.logging_config"].__file__).resolve()
        ),
    }
    close_logging(lifecycle)
    payload["borrowed_handler_still_attached"] = lifecycle.handler in root.handlers
    payload["borrowed_handler_stream_open"] = (
        lifecycle.handler.stream is not None
        and not lifecycle.handler.stream.closed
    )
    payload["log_files"] = sorted(
        str(path.resolve()) for path in sandbox_root.rglob("app.log")
    )
    print(json.dumps(payload))
    self.started = True
    logging.shutdown()

Server.run = inspect_server
main(
    args=[
        "app.main:app",
        "--app-dir",
        str(app_dir),
        "--log-config",
        str(config_path),
    ],
    standalone_mode=False,
)
'''
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["QUESTION_PRACTICE_DATA_DIR"] = str(custom_data_dir)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path),
            str(ROOT / "backend"),
            str(ROOT / "backend" / "uvicorn_logging.json"),
        ],
        cwd=launch_root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert payload == {
        "configured_interval": 1,
        "settings_data_dir": str(custom_data_dir.resolve()),
        "owned": False,
        "root_rotating_handlers": 1,
        "root_handler_paths": [str(custom_log_path.resolve())],
        "when": "MIDNIGHT",
        "runtime_interval": 86_400,
        "backup_count": 30,
        "encoding": "utf-8",
        "logger_handlers": {
            "uvicorn": 0,
            "uvicorn.error": 0,
            "uvicorn.access": 0,
        },
        "logger_propagates": {
            "uvicorn": True,
            "uvicorn.error": True,
            "uvicorn.access": True,
        },
        "marker_counts": {
            "uvicorn": 1,
            "uvicorn.error": 1,
            "uvicorn.access": 1,
        },
        "logging_config_module": str(
            (ROOT / "backend" / "app" / "logging_config.py").resolve()
        ),
        "borrowed_handler_still_attached": True,
        "borrowed_handler_stream_open": True,
        "log_files": [str(custom_log_path.resolve())],
    }
