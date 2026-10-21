from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass

import psutil


IDENTITY_TOLERANCE_SECONDS = 0.01


class ProcessIdentityMismatch(RuntimeError):
    """PID exists but its creation time does not match the recorded process."""


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    started_at: float


@dataclass(frozen=True)
class ProcessTree:
    root: ProcessIdentity
    children: list[ProcessIdentity]


@dataclass(frozen=True)
class TerminationReport:
    verified_pids: list[int]
    terminated: list[int]
    killed: list[int]
    remaining: list[int]


def _verified_process(identity: ProcessIdentity) -> psutil.Process | None:
    try:
        process = psutil.Process(identity.pid)
        actual_started_at = process.create_time()
    except psutil.NoSuchProcess:
        return None
    if abs(actual_started_at - identity.started_at) > IDENTITY_TOLERANCE_SECONDS:
        raise ProcessIdentityMismatch(
            f"PID {identity.pid} 已被复用或启动时间不匹配"
        )
    return process


def inspect_tree(pid: int, started_at: float) -> ProcessTree:
    root_identity = ProcessIdentity(pid=pid, started_at=started_at)
    root = _verified_process(root_identity)
    if root is None:
        raise ProcessIdentityMismatch(f"PID {pid} 已不存在")
    children: list[ProcessIdentity] = []
    for child in root.children(recursive=True):
        try:
            children.append(
                ProcessIdentity(pid=child.pid, started_at=child.create_time())
            )
        except psutil.NoSuchProcess:
            continue
    return ProcessTree(root=root_identity, children=children)


def terminate_tree(
    pid: int,
    started_at: float,
    *,
    terminate_timeout: float = 5.0,
    kill_timeout: float = 5.0,
) -> TerminationReport:
    tree = inspect_tree(pid, started_at)
    identities = [*reversed(tree.children), tree.root]
    verified_pids: list[int] = []
    terminated: list[int] = []
    killed: list[int] = []
    processes: list[psutil.Process] = []
    identity_by_pid = {identity.pid: identity for identity in identities}

    for identity in identities:
        process = _verified_process(identity)
        if process is None:
            continue
        verified_pids.append(identity.pid)
        try:
            process.terminate()
            terminated.append(identity.pid)
            processes.append(process)
        except psutil.NoSuchProcess:
            continue

    _, alive = psutil.wait_procs(processes, timeout=terminate_timeout)
    kill_candidates: list[psutil.Process] = []
    for process in alive:
        identity = identity_by_pid[process.pid]
        current = _verified_process(identity)
        if current is None:
            continue
        try:
            current.kill()
            killed.append(identity.pid)
            kill_candidates.append(current)
        except psutil.NoSuchProcess:
            continue
    psutil.wait_procs(kill_candidates, timeout=kill_timeout)

    remaining: list[int] = []
    for identity in identities:
        try:
            if _verified_process(identity) is not None:
                remaining.append(identity.pid)
        except ProcessIdentityMismatch:
            # A reused PID is unrelated and must never be terminated.
            continue
    return TerminationReport(
        verified_pids=verified_pids,
        terminated=terminated,
        killed=killed,
        remaining=remaining,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="阶段 10 精确进程树控制")
    commands = parser.add_subparsers(dest="command", required=True)
    identity_parser = commands.add_parser("identity")
    identity_parser.add_argument("--pid", type=int, required=True)
    inspect_parser = commands.add_parser("inspect")
    inspect_parser.add_argument("--pid", type=int, required=True)
    inspect_parser.add_argument("--started-at", type=float, required=True)
    terminate_parser = commands.add_parser("terminate")
    terminate_parser.add_argument("--pid", type=int, required=True)
    terminate_parser.add_argument("--started-at", type=float, required=True)
    listener_parser = commands.add_parser("listener")
    listener_parser.add_argument("--pid", type=int, required=True)
    listener_parser.add_argument("--started-at", type=float, required=True)
    listener_parser.add_argument("--host", required=True)
    listener_parser.add_argument("--port", type=int, required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "identity":
            process = psutil.Process(arguments.pid)
            payload = ProcessIdentity(
                pid=process.pid,
                started_at=process.create_time(),
            )
            print(json.dumps(payload.__dict__, sort_keys=True))
            return 0
        if arguments.command == "inspect":
            tree = inspect_tree(arguments.pid, arguments.started_at)
            print(
                json.dumps(
                    {
                        "root": {
                            **tree.root.__dict__,
                            "parent_pid": psutil.Process(tree.root.pid).ppid(),
                        },
                        "children": [
                            {
                                **child.__dict__,
                                "parent_pid": psutil.Process(child.pid).ppid(),
                            }
                            for child in tree.children
                            if psutil.pid_exists(child.pid)
                        ],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "listener":
            identity = ProcessIdentity(arguments.pid, arguments.started_at)
            process = _verified_process(identity)
            if process is None:
                raise ProcessIdentityMismatch(f"PID {arguments.pid} 已不存在")
            listening = any(
                connection.status == psutil.CONN_LISTEN
                and connection.laddr.ip == arguments.host
                and connection.laddr.port == arguments.port
                for connection in process.net_connections(kind="tcp")
            )
            print(json.dumps({"listening": listening}, sort_keys=True))
            return 0 if listening else 4
        report = terminate_tree(arguments.pid, arguments.started_at)
        print(json.dumps(report.__dict__, sort_keys=True))
        return 0 if not report.remaining else 2
    except (ProcessIdentityMismatch, psutil.Error) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
