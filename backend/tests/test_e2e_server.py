import importlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import psutil
import pytest

from app.config import Settings


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_bound_socket_is_handed_to_uvicorn_without_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    e2e_server = importlib.import_module("app.testing.e2e_server")
    captured: dict[str, socket.socket] = {}
    monkeypatch.setattr(
        e2e_server.uvicorn.Server,
        "run",
        lambda _server, sockets: captured.update(socket=sockets[0]),
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing-dist",
        _env_file=None,
    )
    control_path = tmp_path / "control" / "server.json"
    bound = e2e_server.bind_server_socket()
    port = bound.getsockname()[1]

    e2e_server.run_with_socket(bound, settings, control_path)

    assert captured["socket"] is bound
    control = json.loads(control_path.read_text(encoding="utf-8"))
    assert control["host"] == "127.0.0.1"
    assert control["port"] == port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as competing:
        with pytest.raises(OSError):
            competing.bind(("127.0.0.1", port))
    bound.close()


@pytest.mark.skipif(sys.platform != "win32", reason="验证 Windows 实际 socket 接管")
def test_windows_process_keeps_atomic_port_until_uvicorn_serves(
    tmp_path: Path,
) -> None:
    control_path = tmp_path / "control" / "server.json"
    data_dir = tmp_path / "data"
    environment = os.environ.copy()
    environment.update(
        {
            "QUESTION_PRACTICE_DATA_DIR": str(data_dir),
            "QUESTION_PRACTICE_DATABASE_URL": (
                f"sqlite:///{(data_dir / 'app.db').as_posix()}"
            ),
            "QUESTION_PRACTICE_AUTO_CREATE_SCHEMA": "true",
            "QUESTION_PRACTICE_START_BACKGROUND_WORKER": "false",
            "QUESTION_PRACTICE_FRONTEND_DIST": str(tmp_path / "missing-dist"),
            "OCR_ENGINE": "mock",
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "app.testing.e2e_server",
            "--control",
            str(control_path),
        ],
        cwd=BACKEND_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    server_identity: tuple[int, float] | None = None
    try:
        deadline = time.monotonic() + 10
        while not control_path.is_file():
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(f"服务过早退出：{stdout}\n{stderr}")
            assert time.monotonic() < deadline, "等待原子端口控制文件超时"
            time.sleep(0.02)
        control = json.loads(control_path.read_text(encoding="utf-8"))
        server_identity = (int(control["pid"]), float(control["started_at"]))
        process_tree = {
            process.pid,
            *(
                child.pid
                for child in psutil.Process(process.pid).children(recursive=True)
            ),
        }
        assert server_identity[0] in process_tree
        assert control["host"] == "127.0.0.1"
        port = int(control["port"])

        health_url = f"http://127.0.0.1:{port}/api/health"
        while True:
            try:
                with urllib.request.urlopen(health_url, timeout=0.5) as response:
                    assert response.status == 200
                break
            except OSError:
                assert time.monotonic() < deadline, "等待 FastAPI 健康检查超时"
                time.sleep(0.02)

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as competing:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                competing.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            with pytest.raises(OSError):
                competing.bind(("127.0.0.1", port))

        process.terminate()
        process.wait(timeout=10)
        process.communicate(timeout=1)
        if psutil.pid_exists(server_identity[0]):
            server_process = psutil.Process(server_identity[0])
            assert abs(server_process.create_time() - server_identity[1]) <= 0.01
            server_process.terminate()
            server_process.wait(timeout=10)
        with pytest.raises(OSError):
            urllib.request.urlopen(health_url, timeout=0.5)
        while True:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as released:
                    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                        released.setsockopt(
                            socket.SOL_SOCKET,
                            socket.SO_EXCLUSIVEADDRUSE,
                            1,
                        )
                    released.bind(("127.0.0.1", port))
                break
            except OSError:
                assert time.monotonic() < deadline, "等待原子端口释放超时"
                time.sleep(0.02)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.communicate(timeout=1)
        if server_identity and psutil.pid_exists(server_identity[0]):
            server_process = psutil.Process(server_identity[0])
            if abs(server_process.create_time() - server_identity[1]) <= 0.01:
                server_process.kill()
                server_process.wait(timeout=10)
