from __future__ import annotations

import argparse
import json
import os
import socket
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

import psutil
import uvicorn

from app.config import Settings
from app.main import create_app


@dataclass(frozen=True)
class ServerControl:
    pid: int
    started_at: float
    host: str
    port: int
    shutdown_path: str


def bind_server_socket(host: str = "127.0.0.1") -> socket.socket:
    if host != "127.0.0.1":
        raise ValueError("E2E 服务只允许绑定 127.0.0.1")
    bound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            bound.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        bound.bind((host, 0))
        bound.listen(2048)
        return bound
    except Exception:
        bound.close()
        raise


def write_control_file(path: Path, control: ServerControl) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(asdict(control), stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def run_with_socket(
    bound: socket.socket,
    settings: Settings,
    control_path: Path,
) -> None:
    host, port = bound.getsockname()[:2]
    shutdown_path = control_path.with_suffix(".shutdown")
    shutdown_path.unlink(missing_ok=True)
    control = ServerControl(
        pid=os.getpid(),
        started_at=psutil.Process(os.getpid()).create_time(),
        host=str(host),
        port=int(port),
        shutdown_path=str(shutdown_path),
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings),
            host=settings.host,
            port=0,
            log_level="info",
            access_log=True,
        )
    )
    write_control_file(control_path, control)
    monitor_stop = threading.Event()

    def watch_shutdown() -> None:
        while not monitor_stop.wait(0.05):
            if shutdown_path.is_file():
                server.should_exit = True
                return

    monitor = threading.Thread(
        target=watch_shutdown,
        name="e2e-graceful-shutdown",
        daemon=True,
    )
    monitor.start()
    try:
        server.run(sockets=[bound])
    finally:
        monitor_stop.set()
        monitor.join(timeout=1)
        shutdown_path.unlink(missing_ok=True)


def run_server(control_path: Path) -> int:
    settings = Settings(_env_file=None)
    bound = bind_server_socket(settings.host)
    try:
        run_with_socket(bound, settings, control_path)
    finally:
        bound.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动隔离的 E2E FastAPI 服务")
    parser.add_argument("--control", type=Path, required=True)
    arguments = parser.parse_args(argv)
    return run_server(arguments.control)


if __name__ == "__main__":
    raise SystemExit(main())
