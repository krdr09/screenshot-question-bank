@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

if not exist "backend\.venv\Scripts\python.exe" (
  echo [错误] 请先运行 install.bat。
  pause
  exit /b 1
)

start "题库后端" cmd /k "backend\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8765 --reload"
start "题库前端" cmd /k "cd /d frontend && pnpm dev"

