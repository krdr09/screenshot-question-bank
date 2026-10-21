from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Literal

from app.services.backups.recovery import RestoreRecoveryState


AvailabilityMode = Literal[
    "ready", "restart_required", "recovery_required"
]
UNKNOWN_RECOVERY_STATE = RestoreRecoveryState(None, None, None, None)


@dataclass(frozen=True)
class ServiceAvailabilitySnapshot:
    mode: AvailabilityMode
    recovery: RestoreRecoveryState | None


class ServiceAvailabilityState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = ServiceAvailabilitySnapshot("ready", None)

    def snapshot(self) -> ServiceAvailabilitySnapshot:
        with self._lock:
            return self._snapshot

    def require_restart(self) -> None:
        with self._lock:
            if self._snapshot.mode == "recovery_required":
                return
            self._snapshot = ServiceAvailabilitySnapshot(
                "restart_required", None
            )

    def require_recovery(self, state: RestoreRecoveryState) -> None:
        with self._lock:
            self._snapshot = ServiceAvailabilitySnapshot(
                "recovery_required", state
            )


def fail_closed_recovery(snapshot: object) -> RestoreRecoveryState | None:
    mode = getattr(snapshot, "mode", None)
    recovery = getattr(snapshot, "recovery", None)
    if mode == "recovery_required":
        return (
            recovery
            if isinstance(recovery, RestoreRecoveryState)
            else UNKNOWN_RECOVERY_STATE
        )
    if mode not in {"ready", "restart_required"} or recovery is not None:
        return UNKNOWN_RECOVERY_STATE
    return None
