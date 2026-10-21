@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHON=backend\.venv\Scripts\python.exe"
if defined QUESTION_PRACTICE_PYTHON set "PYTHON=%QUESTION_PRACTICE_PYTHON%"
set "PORT=8765"
if defined QUESTION_PRACTICE_PORT set "PORT=%QUESTION_PRACTICE_PORT%"
set "URL=http://127.0.0.1:%PORT%"
set "OPEN_URL=%URL%/"
if defined QUESTION_PRACTICE_STARTUP_PROBE set "OPEN_URL=%URL%/?startup_probe=%QUESTION_PRACTICE_STARTUP_PROBE%"

powershell -NoProfile -Command "$port=0; if(-not [int]::TryParse('%PORT%', [ref]$port) -or $port -lt 1 -or $port -gt 65535){exit 1}"
if errorlevel 1 (
  echo [错误] QUESTION_PRACTICE_PORT 必须是 1 到 65535 的整数。
  exit /b 1
)

if not exist "%PYTHON%" (
  echo [错误] 未找到虚拟环境，请先运行 install.bat。
  if not defined QUESTION_PRACTICE_NONINTERACTIVE pause
  exit /b 1
)

if not exist "frontend\dist\index.html" (
  echo [错误] 未找到前端构建产物，请先运行 install.bat。
  if not defined QUESTION_PRACTICE_NONINTERACTIVE pause
  exit /b 1
)

echo 正在检查数据库迁移...
"%PYTHON%" -m alembic -c backend\alembic.ini upgrade head
if errorlevel 1 (
  echo [错误] 数据库迁移失败，请查看上方信息和 data\logs。
    if not defined QUESTION_PRACTICE_NONINTERACTIVE pause
  exit /b 1
)

if /I "%~1"=="--check" (
  echo 启动前检查通过：%URL%
  exit /b 0
)

if not exist "data\logs" mkdir "data\logs"
echo 正在启动：%URL%

start "" /B powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File scripts\open-app-when-ready.ps1 -Url "%URL%" -OpenUrl "%OPEN_URL%"

"%PYTHON%" -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port %PORT% --log-config backend\uvicorn_logging.json
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
  echo [错误] 服务异常退出，请查看上方信息或 data\logs\app.log。
  if not defined QUESTION_PRACTICE_NONINTERACTIVE pause
)
exit /b %EXIT_CODE%

