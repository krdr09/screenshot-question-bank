import logging
from dataclasses import dataclass, field
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from threading import RLock


@dataclass(frozen=True, slots=True)
class LoggingHandle:
    handler: TimedRotatingFileHandler
    owned: bool
    _target: Path | None = field(default=None, repr=False)
    _lease: object | None = field(default=None, repr=False)


@dataclass(slots=True)
class _OwnedHandlerState:
    handler: TimedRotatingFileHandler
    leases: set[object]


_handler_lock = RLock()
_owned_handlers: dict[Path, _OwnedHandlerState] = {}


def _normalized_log_path(path: str | Path) -> Path:
    return Path(path).resolve(strict=False)


def create_uvicorn_log_handler(
    *,
    when: str,
    interval: int,
    backupCount: int,
    encoding: str,
) -> TimedRotatingFileHandler:
    """Create the process-owned handler from the application's settings."""

    from app.config import Settings

    log_dir = Settings().data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return TimedRotatingFileHandler(
        _normalized_log_path(log_dir / "app.log"),
        when=when,
        interval=interval,
        backupCount=backupCount,
        encoding=encoding,
    )


def configure_logging(log_dir: Path) -> LoggingHandle:
    """Configure or borrow the daily rotating application log handler."""

    log_dir.mkdir(parents=True, exist_ok=True)
    target = _normalized_log_path(log_dir / "app.log")
    root = logging.getLogger()

    with _handler_lock:
        root.setLevel(logging.INFO)
        owned_state = _owned_handlers.get(target)
        if owned_state is not None:
            lease = object()
            owned_state.leases.add(lease)
            return LoggingHandle(
                handler=owned_state.handler,
                owned=True,
                _target=target,
                _lease=lease,
            )

        for candidate in root.handlers:
            if (
                isinstance(candidate, TimedRotatingFileHandler)
                and _normalized_log_path(candidate.baseFilename) == target
            ):
                return LoggingHandle(handler=candidate, owned=False)

        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler = TimedRotatingFileHandler(
            target,
            when="midnight",
            interval=1,
            backupCount=30,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)

        lease = object()
        _owned_handlers[target] = _OwnedHandlerState(
            handler=file_handler,
            leases={lease},
        )
        root.addHandler(file_handler)
        return LoggingHandle(
            handler=file_handler,
            owned=True,
            _target=target,
            _lease=lease,
        )


def close_logging(handle: LoggingHandle) -> None:
    if not handle.owned:
        return

    with _handler_lock:
        if handle._target is None or handle._lease is None:
            return
        owned_state = _owned_handlers.get(handle._target)
        if (
            owned_state is None
            or owned_state.handler is not handle.handler
            or handle._lease not in owned_state.leases
        ):
            return

        owned_state.leases.remove(handle._lease)
        if owned_state.leases:
            return

        del _owned_handlers[handle._target]
        root = logging.getLogger()
        if handle.handler in root.handlers:
            root.removeHandler(handle.handler)
        handle.handler.close()
