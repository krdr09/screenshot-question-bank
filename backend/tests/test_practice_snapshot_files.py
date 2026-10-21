import pytest

from app.services.practice.snapshot_files import (
    PracticeSnapshotReferenceError,
    collect_practice_snapshot_crop_paths,
)


def test_collect_practice_snapshot_crop_paths_deduplicates_valid_paths() -> None:
    snapshots = [
        [
            {"part_index": 0, "crop_path": "crops/one.png"},
            {"part_index": 1, "crop_path": "crops/nested/two.png"},
        ],
        [{"part_index": 0, "crop_path": "crops/one.png"}],
        [],
    ]

    assert collect_practice_snapshot_crop_paths(snapshots) == {
        "crops/one.png",
        "crops/nested/two.png",
    }


@pytest.mark.parametrize(
    "snapshot",
    [
        {"crop_path": "crops/not-a-list.png"},
        ["not-an-object"],
        [{}],
        [{"crop_path": 123}],
        [{"crop_path": "imports/wrong-root.png"}],
        [{"crop_path": "../outside.png"}],
        [{"crop_path": "/crops/absolute.png"}],
        [{"crop_path": "crops\\windows.png"}],
        [{"crop_path": "crops/../outside.png"}],
        [{"crop_path": "crops//empty.png"}],
    ],
)
def test_collect_practice_snapshot_crop_paths_rejects_invalid_metadata(snapshot) -> None:
    with pytest.raises(PracticeSnapshotReferenceError):
        collect_practice_snapshot_crop_paths([snapshot])
