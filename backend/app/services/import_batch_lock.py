from __future__ import annotations

from contextlib import contextmanager
from threading import Lock, RLock
from typing import Iterator


_registry_lock = Lock()
_batch_locks: dict[str, RLock] = {}


@contextmanager
def import_batch_lock(batch_id: str) -> Iterator[None]:
    with _registry_lock:
        lock = _batch_locks.setdefault(batch_id, RLock())
    with lock:
        yield
