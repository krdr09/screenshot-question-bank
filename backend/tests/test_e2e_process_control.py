from __future__ import annotations

import json
import subprocess
import sys
import time

import psutil
import pytest

from app.testing.process_control import (
    ProcessIdentityMismatch,
    inspect_tree,
    terminate_tree,
)


@pytest.mark.skipif(sys.platform != "win32", reason="验证 Windows 精确进程树管理")
def test_process_tree_requires_pid_and_start_time_before_termination() -> None:
    sleeper = "import time; time.sleep(60)"
    root = subprocess.Popen(
        [
            sys._base_executable,
            "-c",
            (
                "import subprocess,sys,time;"
                f"subprocess.Popen([sys.executable,'-c',{sleeper!r}]);"
                "time.sleep(60)"
            ),
        ]
    )
    started_at = psutil.Process(root.pid).create_time()
    try:
        deadline = time.monotonic() + 5
        while not psutil.Process(root.pid).children(recursive=True):
            assert time.monotonic() < deadline, "等待测试子进程超时"
            time.sleep(0.01)

        tree = inspect_tree(root.pid, started_at)

        assert tree.root.pid == root.pid
        assert tree.root.started_at == pytest.approx(started_at, abs=0.01)
        assert len(tree.children) == 1
        with pytest.raises(ProcessIdentityMismatch):
            terminate_tree(root.pid, started_at - 10_000)
        assert psutil.pid_exists(root.pid)

        report = terminate_tree(root.pid, started_at)

        assert root.pid in report.verified_pids
        assert report.remaining == []
        assert not psutil.pid_exists(root.pid)
        assert all(not psutil.pid_exists(identity.pid) for identity in tree.children)
    finally:
        if psutil.pid_exists(root.pid):
            process = psutil.Process(root.pid)
            for child in process.children(recursive=True):
                child.kill()
            process.kill()
        root.wait(timeout=10)


def test_inspect_tree_rejects_a_reused_or_mismatched_pid() -> None:
    current = psutil.Process()

    with pytest.raises(ProcessIdentityMismatch):
        inspect_tree(current.pid, current.create_time() - 1)


def test_process_control_cli_reports_machine_readable_identity() -> None:
    current = psutil.Process()

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.testing.process_control",
            "identity",
            "--pid",
            str(current.pid),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "pid": current.pid,
        "started_at": pytest.approx(current.create_time(), abs=0.01),
    }
