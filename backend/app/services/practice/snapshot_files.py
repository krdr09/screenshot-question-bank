from __future__ import annotations

from collections.abc import Iterable
from pathlib import PurePosixPath


class PracticeSnapshotReferenceError(ValueError):
    pass


def collect_practice_snapshot_crop_paths(snapshots: Iterable[object]) -> set[str]:
    paths: set[str] = set()
    for snapshot in snapshots:
        if not isinstance(snapshot, list):
            raise PracticeSnapshotReferenceError("invalid practice image snapshot")
        for part in snapshot:
            if not isinstance(part, dict):
                raise PracticeSnapshotReferenceError("invalid practice image snapshot")
            raw_path = part.get("crop_path")
            if not isinstance(raw_path, str) or "\\" in raw_path:
                raise PracticeSnapshotReferenceError("invalid practice image snapshot")
            segments = raw_path.split("/")
            pure = PurePosixPath(raw_path)
            if (
                pure.is_absolute()
                or not segments
                or segments[0] != "crops"
                or any(segment in {"", ".", ".."} for segment in segments)
            ):
                raise PracticeSnapshotReferenceError("invalid practice image snapshot")
            paths.add(pure.as_posix())
    return paths
