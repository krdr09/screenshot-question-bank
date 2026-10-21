from __future__ import annotations

import logging
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable

from app.services.backups.errors import BackupTokenExpiredError
from app.services.backups.validation import ValidatedBackup


logger = logging.getLogger(__name__)
MAX_CLEANUP_INTERVAL_SECONDS = 60.0
CLEANUP_JOIN_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class TokenEntry:
    validated: ValidatedBackup
    expires_at: float


class _CleanupState(Enum):
    STOPPED = auto()
    RUNNING = auto()
    STOPPING = auto()


class RestoreTokenRegistry:
    def __init__(
        self,
        ttl_seconds: int,
        clock: Callable[[], float] = time.monotonic,
        *,
        cleanup_interval_seconds: float | None = None,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self.cleanup_interval_seconds = (
            float(cleanup_interval_seconds)
            if cleanup_interval_seconds is not None
            else min(float(ttl_seconds), MAX_CLEANUP_INTERVAL_SECONDS)
        )
        if self.cleanup_interval_seconds <= 0:
            raise ValueError("cleanup interval must be positive")
        self._entries: dict[str, TokenEntry] = {}
        self._lock = threading.Lock()
        self._cleanup_stop: threading.Event | None = None
        self._cleanup_lifecycle_lock = threading.Lock()
        self._cleanup_thread: threading.Thread | None = None
        self._cleanup_state = _CleanupState.STOPPED
        self._cleanup_generation = 0
        self._cleanup_stoppers = 0

    def start_cleanup(self) -> None:
        with self._cleanup_lifecycle_lock:
            current_thread = self._cleanup_thread
            if self._cleanup_state is _CleanupState.RUNNING:
                if current_thread is not None and current_thread.is_alive():
                    return
                if current_thread is not None:
                    self._clear_cleanup_generation_locked(
                        current_thread,
                        self._cleanup_generation,
                    )
            elif self._cleanup_state is _CleanupState.STOPPING:
                if (
                    current_thread is not None
                    and (
                        current_thread.is_alive()
                        or self._cleanup_stoppers > 0
                    )
                ):
                    raise RuntimeError(
                        "restore token cleanup thread is still stopping"
                    )
                if current_thread is not None:
                    self._clear_cleanup_generation_locked(
                        current_thread,
                        self._cleanup_generation,
                    )

            generation = self._cleanup_generation + 1
            stop_event = threading.Event()
            published = threading.Event()
            thread = threading.Thread(
                target=self._run_cleanup,
                args=(stop_event, published),
                name="restore-token-cleanup",
                daemon=True,
            )
            thread.start()
            self._cleanup_generation = generation
            self._cleanup_stop = stop_event
            self._cleanup_thread = thread
            self._cleanup_state = _CleanupState.RUNNING
            published.set()

    def _run_cleanup(
        self,
        stop_event: threading.Event,
        published: threading.Event,
    ) -> None:
        published.wait()
        while not stop_event.wait(self.cleanup_interval_seconds):
            try:
                self.cleanup_expired()
            except Exception:
                logger.exception("过期恢复令牌暂存目录清理失败")

    def stop_cleanup(self) -> None:
        with self._cleanup_lifecycle_lock:
            thread = self._cleanup_thread
            if thread is None:
                return
            generation = self._cleanup_generation
            stop_event = self._cleanup_stop
            if stop_event is None:
                raise RuntimeError("cleanup thread has no stop event")
            self._cleanup_state = _CleanupState.STOPPING
            self._cleanup_stoppers += 1
            stop_event.set()
        try:
            thread.join(timeout=CLEANUP_JOIN_TIMEOUT_SECONDS)
            if thread.is_alive():
                logger.error(
                    "恢复令牌清理线程未能在 %.1f 秒内停止（generation=%d）",
                    CLEANUP_JOIN_TIMEOUT_SECONDS,
                    generation,
                )
        finally:
            with self._cleanup_lifecycle_lock:
                if (
                    self._cleanup_thread is thread
                    and self._cleanup_generation == generation
                ):
                    self._cleanup_stoppers -= 1
                    if not thread.is_alive() and self._cleanup_stoppers == 0:
                        self._clear_cleanup_generation_locked(thread, generation)

    def _clear_cleanup_generation_locked(
        self,
        thread: threading.Thread,
        generation: int,
    ) -> None:
        if (
            self._cleanup_thread is not thread
            or self._cleanup_generation != generation
        ):
            return
        self._cleanup_thread = None
        self._cleanup_stop = None
        self._cleanup_state = _CleanupState.STOPPED
        self._cleanup_stoppers = 0

    def issue(self, validated: ValidatedBackup) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._entries[token] = TokenEntry(
                validated=validated,
                expires_at=self.clock() + self.ttl_seconds,
            )
        return token

    def consume(self, token: str) -> ValidatedBackup:
        with self._lock:
            entry = self._entries.pop(token, None)
        if entry is None:
            raise BackupTokenExpiredError()
        if entry.expires_at <= self.clock():
            self.discard_validated(entry.validated)
            raise BackupTokenExpiredError()
        return entry.validated

    def discard(self, token: str) -> None:
        with self._lock:
            entry = self._entries.pop(token, None)
        if entry is not None:
            self.discard_validated(entry.validated)

    @staticmethod
    def discard_validated(validated: ValidatedBackup) -> None:
        shutil.rmtree(validated.staging_dir, ignore_errors=True)

    def cleanup_expired(self) -> None:
        now = self.clock()
        with self._lock:
            expired_tokens = [
                token
                for token, entry in self._entries.items()
                if entry.expires_at <= now
            ]
            expired = [
                self._entries.pop(token).validated for token in expired_tokens
            ]
        for validated in expired:
            self.discard_validated(validated)
