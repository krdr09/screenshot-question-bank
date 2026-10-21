from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from threading import RLock


_practice_image_lifecycle_lock = RLock()


@contextmanager
def practice_image_lifecycle() -> Iterator[None]:
    with _practice_image_lifecycle_lock:
        yield
