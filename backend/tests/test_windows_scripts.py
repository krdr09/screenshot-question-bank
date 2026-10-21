import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def read_script(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def read_script_bytes(name: str) -> bytes:
    return (ROOT / name).read_bytes()


@pytest.mark.parametrize("name", ["install.bat", "start.bat"])
def test_batch_scripts_are_utf8_bom_with_crlf(name: str) -> None:
    payload = read_script_bytes(name)

    assert payload.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in payload
    assert b"\n" not in payload.replace(b"\r\n", b"")


@pytest.mark.parametrize(
    "name", ["scripts/open-app-when-ready.ps1", "scripts/verify-windows.ps1"]
)
def test_windows_powershell_scripts_are_utf8_bom_with_crlf(name: str) -> None:
    payload = read_script_bytes(name)

    assert payload.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in payload
    assert b"\n" not in payload.replace(b"\r\n", b"")


def test_windows_verifier_retries_idempotent_long_path_tree_cleanup() -> None:
    script = read_script("scripts/verify-windows.ps1")

    assert "$extendedPath = '\\\\?\\' + $verified" in script
    assert "[IO.Directory]::Delete($extendedPath, $true)" in script
    assert "for ($attempt = 1; $attempt -le 5; $attempt++)" in script
    assert "if (-not (Test-Path -LiteralPath $verified)) { return }" in script
    assert "Start-Sleep -Milliseconds (100 * $attempt)" in script
    assert "验收目录清理失败" in script


def test_windows_verifier_stops_the_verified_process_handle_idempotently() -> None:
    script = read_script("scripts/verify-windows.ps1")
    stop_identity = script.split("function Stop-ExactIdentity", 1)[1].split(
        "function Stop-ExactProcessTree", 1
    )[0]

    assert "$process = Get-Process -Id ([int]$Identity.Pid) -ErrorAction Stop" in stop_identity
    assert "$handle = $process.SafeHandle" in stop_identity
    assert "$process.Kill()" in stop_identity
    assert "$process.HasExited" in stop_identity
    assert "Stop-Process -Id" not in stop_identity


def test_start_script_never_binds_all_interfaces() -> None:
    script = read_script("start.bat")
    browser_helper = read_script("scripts/open-app-when-ready.ps1")

    assert "--host 127.0.0.1" in script
    assert "--host 0.0.0.0" not in script
    assert "/api/health" in browser_helper


def test_install_script_checks_each_major_step() -> None:
    script = read_script("install.bat")

    assert "python --version" in script
    assert "node --version" in script
    assert "pip install" in script
    assert "pnpm install" in script
    assert "pnpm build" in script
    assert "-m alembic -c backend\\alembic.ini upgrade head" in script
    assert script.count("if errorlevel 1") >= 5


def test_install_script_calls_cmd_shims_without_abandoning_the_batch() -> None:
    script = read_script("install.bat")
    verifier = read_script("scripts/verify-windows.ps1")

    assert "call pnpm --version" in script
    assert "call pnpm install --frozen-lockfile" in script
    assert "call pnpm build" in script
    assert 'chcp 65001>nul & call install.bat' in verifier
    assert 'chcp 65001>nul & call start.bat' in verifier


def test_install_script_creates_configured_data_dir_before_migration() -> None:
    script = read_script("install.bat")

    create_data_dir = script.index(
        'from app.config import Settings; Settings().data_dir.mkdir(parents=True, exist_ok=True)'
    )
    error_check = script.index("if errorlevel 1 goto :failed", create_data_dir)
    migration = script.index("-m alembic -c backend\\alembic.ini upgrade head")
    assert create_data_dir < error_check < migration


def test_install_script_includes_local_cpu_ocr_dependencies() -> None:
    script = read_script("install.bat")

    assert '"backend[ocr-cpu]"' in script
    assert "-m app.ocr.install_check --device cpu" in script
    assert "OCR CPU 就绪检查" in script


def test_start_script_applies_database_migrations_before_launch() -> None:
    script = read_script("start.bat")

    migration = script.index("-m alembic -c backend\\alembic.ini upgrade head")
    launch = script.index("-m uvicorn")
    assert migration < launch


def test_start_script_supports_loopback_port_and_unique_browser_probe() -> None:
    script = read_script("start.bat")

    assert "QUESTION_PRACTICE_PORT" in script
    assert 'set "URL=http://127.0.0.1:%PORT%"' in script
    assert 'set "OPEN_URL=%URL%/"' in script
    assert "QUESTION_PRACTICE_STARTUP_PROBE" in script
    assert "/?startup_probe=%QUESTION_PRACTICE_STARTUP_PROBE%" in script


def test_start_script_delegates_single_browser_launch_after_health_check() -> None:
    script = read_script("start.bat")
    browser_helper = read_script("scripts/open-app-when-ready.ps1")

    assert "-ExecutionPolicy Bypass" in script
    assert "-File scripts\\open-app-when-ready.ps1" in script
    assert "rundll32.exe" not in script
    assert "& explorer.exe $openUrl" not in script
    assert "Start-Process $openUrl" not in script
    assert '[string]$UrlHandler = "rundll32.exe"' in browser_helper
    assert '& $UrlHandler "url.dll,FileProtocolHandler" $OpenUrl' in browser_helper


@pytest.mark.skipif(os.name != "nt", reason="requires Windows PowerShell and cmd")
def test_browser_ready_helper_does_not_retry_successful_url_handoff(
    tmp_path: Path,
) -> None:
    calls_path = tmp_path / "url-handler-calls.txt"
    handler_path = tmp_path / "fake-url-handler.cmd"
    handler_path.write_text(
        '@echo off\r\necho called>>"%~dp0url-handler-calls.txt"\r\nexit /b 7\r\n',
        encoding="utf-8",
        newline="",
    )

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.server.request_count += 1  # type: ignore[attr-defined]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
    server.request_count = 0  # type: ignore[attr-defined]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "scripts" / "open-app-when-ready.ps1"),
                "-Url",
                f"http://127.0.0.1:{server.server_port}",
                "-OpenUrl",
                "http://127.0.0.1/example",
                "-UrlHandler",
                str(handler_path),
                "-MaxAttempts",
                "3",
                "-RetryDelayMilliseconds",
                "10",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    assert result.returncode == 0, result.stdout + result.stderr
    assert calls_path.read_text(encoding="utf-8").splitlines() == ["called"]
    assert server.request_count == 1  # type: ignore[attr-defined]


@pytest.mark.skipif(os.name != "nt", reason="requires Windows PowerShell and cmd")
def test_browser_ready_helper_does_not_launch_when_health_never_succeeds(
    tmp_path: Path,
) -> None:
    calls_path = tmp_path / "url-handler-calls.txt"
    handler_path = tmp_path / "fake-url-handler.cmd"
    handler_path.write_text(
        '@echo off\r\necho called>>"%~dp0url-handler-calls.txt"\r\nexit /b 0\r\n',
        encoding="utf-8",
        newline="",
    )

    class UnavailableHealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.server.request_count += 1  # type: ignore[attr-defined]
            self.send_response(503)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), UnavailableHealthHandler)
    server.request_count = 0  # type: ignore[attr-defined]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "scripts" / "open-app-when-ready.ps1"),
                "-Url",
                f"http://127.0.0.1:{server.server_port}",
                "-OpenUrl",
                "http://127.0.0.1/example",
                "-UrlHandler",
                str(handler_path),
                "-MaxAttempts",
                "2",
                "-RetryDelayMilliseconds",
                "10",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    assert result.returncode == 1, result.stdout + result.stderr
    assert not calls_path.exists()
    assert server.request_count == 2  # type: ignore[attr-defined]


def test_windows_scripts_never_use_global_process_name_cleanup() -> None:
    combined = "\n".join(
        read_script(name) for name in ["install.bat", "start.bat", "scripts/verify-windows.ps1"]
        if (ROOT / name).exists()
    ).lower()

    assert "taskkill /im" not in combined
    assert "stop-process -name" not in combined
    assert "get-process python" not in combined
    assert "get-process node" not in combined
    assert "get-process msedge" not in combined


def test_full_verifier_uses_explicit_noninteractive_batch_mode() -> None:
    install = read_script("install.bat")
    start = read_script("start.bat")
    verifier = read_script("scripts/verify-windows.ps1")

    assert "QUESTION_PRACTICE_NONINTERACTIVE" in install
    assert "QUESTION_PRACTICE_NONINTERACTIVE" in start
    assert '$env:QUESTION_PRACTICE_NONINTERACTIVE = "1"' in verifier


def test_cpu_install_check_runs_real_generated_image_recognition() -> None:
    source_path = ROOT / "backend" / "app" / "ocr" / "install_check.py"

    assert source_path.is_file()
    source = source_path.read_text(encoding="utf-8")
    assert "PaddleOcrEngine" in source
    assert 'requested_device=args.device' in source
    assert "Image.new" in source
    assert ".recognize(" in source
    assert "MockOcrEngine" not in source


def test_windows_verifier_uses_tracked_isolation_and_exact_process_identity() -> None:
    verifier_path = ROOT / "scripts" / "verify-windows.ps1"

    assert verifier_path.is_file()
    script = verifier_path.read_text(encoding="utf-8")
    lowered = script.lower()
    assert "git ls-files -z" in lowered
    assert ".Split([char[]]@([char]0), [StringSplitOptions]::RemoveEmptyEntries)" in script
    assert "git status --porcelain" in lowered
    assert "install.bat" in script
    assert "start.bat" in script
    assert "startup_probe" in script
    assert "Get-CimInstance" in script
    assert "Win32_Process" in script
    assert "StartTime" in script
    assert "Get-Process -Id" in script
    assert "Assert-ExactIdentityCurrent" in script
    assert "$candidateIdentity.StartTime -lt $parentIdentity.StartTime" in script
    stop_tree = script[script.index("function Stop-ExactProcessTree") :]
    first_root_check = stop_tree.index("Assert-ExactIdentityCurrent -Identity $RootIdentity")
    descendant_snapshot = stop_tree.index("Get-DescendantIdentities")
    assert first_root_check < descendant_snapshot
    assert stop_tree.count("Assert-ExactIdentityCurrent -Identity $RootIdentity") >= 2
    assert "Assert-ContainedPath" in script
    assert "Resolve-Path -LiteralPath" in script
    assert "Remove-Item" in script
    assert "127.0.0.1" in script
    assert "/api/health" in script
    assert "/api/this-route-must-not-exist" in script
    assert "git ls-files" in script
    assert "GIT_INDEX_FILE" in script
    assert "GIT_WORK_TREE" in script
    assert "read-tree HEAD" in script
    assert "checkout-index" in script
    assert "checkout-index --force --stdin -z" in script
    assert "WriteAllBytes" in script
    assert "tracked-files.nul" in script
    assert "Start-CmdCommand" in script
    assert "Start-Process -FilePath" not in script
    assert "StandardInput.BaseStream" not in script
    assert "StandardInputEncoding" not in script
    assert "Assert-WindowsScriptEncoding" in script
    assert 'foreach ($windowsScript in @("install.bat", "start.bat", "scripts/open-app-when-ready.ps1", "scripts/verify-windows.ps1"))' in script
    assert "Copy-Item -LiteralPath $source" not in script
    assert "未提交" in script
    assert "install-identity.json" in script
    assert "Wait-ExactProcessExit" in script
    assert "$script:activeRootIdentity" in script
    assert "$script:activeObservedIdentities" in script
    assert "finally" in script
    assert "pip-check.stdout.log" in script
    assert "-m pip check" in script
    assert "pip-check.stderr.log" in script
    assert "taskkill /im" not in lowered
    assert "stop-process -name" not in lowered


def test_windows_verifier_requires_all_runtime_sources_to_be_tracked() -> None:
    script = read_script("scripts/verify-windows.ps1")

    for required in [
        "install.bat",
        "start.bat",
        "scripts/open-app-when-ready.ps1",
        "backend/pyproject.toml",
        "backend/alembic.ini",
        "frontend/package.json",
        "frontend/pnpm-lock.yaml",
        "backend/app/ocr/install_check.py",
        ".gitattributes",
    ]:
        assert required in script


def test_windows_verifier_uses_short_git_common_workspace_run_root() -> None:
    script = read_script("scripts/verify-windows.ps1")

    assert "rev-parse --path-format=absolute --git-common-dir" in script
    assert '$acceptanceRoot = Join-Path $workspaceRoot ".acceptance-runs"' in script
    assert '$acceptanceRoot = Join-Path $repoRoot ".acceptance-runs"' not in script


def test_start_script_routes_uvicorn_to_rotating_app_log() -> None:
    script = read_script("start.bat")
    launch = next(line for line in script.splitlines() if "-m uvicorn" in line)

    assert script.index('if not exist "data\\logs" mkdir "data\\logs"') < script.index(
        launch
    )
    assert "--log-config backend\\uvicorn_logging.json" in launch
    assert "console.log" not in launch
    assert ">>" not in launch
    assert "data\\logs\\app.log" in script


def test_windows_verifier_copies_logging_config_and_reads_rotating_probe() -> None:
    script = read_script("scripts/verify-windows.ps1")

    assert '"backend/uvicorn_logging.json"' in script
    assert '$accessLog = Join-Path $copyRoot "data/logs/app.log"' in script
    assert 'Join-Path $copyRoot "data/logs/console.log"' not in script


def test_windows_verifier_rejects_repeated_startup_probe_requests() -> None:
    script = read_script("scripts/verify-windows.ps1")

    assert "$probeMatches.Count -eq 1" in script
    assert "$probeMatches.Count -gt 1" in script
    assert "启动探针被重复访问" in script
