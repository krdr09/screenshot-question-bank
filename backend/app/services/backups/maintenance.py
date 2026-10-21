from __future__ import annotations

import threading
from collections import Counter
from contextlib import contextmanager
from collections.abc import Iterator

from app.services.backups.errors import BackupBusyError


class MaintenanceCoordinator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._activities: Counter[str] = Counter()
        self._exclusive_label: str | None = None

    @contextmanager
    def activity(self, label: str) -> Iterator[None]:
        with self._lock:
            if self._exclusive_label is not None:
                raise BackupBusyError(
                    f"系统正在{self._exclusive_label}，请稍后重试"
                )
            self._activities[label] += 1
        try:
            yield
        finally:
            with self._lock:
                self._activities[label] -= 1
                if self._activities[label] <= 0:
                    del self._activities[label]

    @contextmanager
    def exclusive(self, label: str) -> Iterator[None]:
        with self._lock:
            if self._exclusive_label is not None:
                raise BackupBusyError(
                    f"系统正在{self._exclusive_label}，请稍后重试"
                )
            if self._activities:
                active = next(iter(self._activities))
                raise BackupBusyError(f"系统正在{active}，请稍后重试")
            self._exclusive_label = label
        try:
            yield
        finally:
            with self._lock:
                self._exclusive_label = None
