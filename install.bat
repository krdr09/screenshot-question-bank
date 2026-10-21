@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

echo [1/8] 检查 Python 3.11 或 3.12...
where python >nul 2>nul
if errorlevel 1 goto :missing_python
python --version
if errorlevel 1 goto :failed
python -c "import sys; raise SystemExit(0 if sys.version_info[:2] in ((3,11),(3,12)) else 1)"
if errorlevel 1 goto :unsupported_python

echo [2/8] 检查 Node.js...
where node >nul 2>nul
if errorlevel 1 goto :missing_node
node --version
if errorlevel 1 goto :failed

echo [3/8] 检查 pnpm...
where pnpm >nul 2>nul
if errorlevel 1 goto :missing_pnpm
call pnpm --version
if errorlevel 1 goto :failed

if /I "%~1"=="--check" goto :check_ok

echo [4/8] 创建 Python 虚拟环境并安装后端依赖...
if not exist "backend\.venv\Scripts\python.exe" python -m venv "backend\.venv"
if errorlevel 1 goto :failed
"backend\.venv\Scripts\python.exe" -m pip install --disable-pip-version-check -e "backend[ocr-cpu]"
if errorlevel 1 goto :failed

echo [5/8] 执行真实 OCR CPU 就绪检查...
set "PYTHONPATH=%CD%\backend;%PYTHONPATH%"
"backend\.venv\Scripts\python.exe" -m app.ocr.install_check --device cpu
if errorlevel 1 goto :failed

echo [6/8] 安装前端依赖...
pushd frontend
call pnpm install --frozen-lockfile
if errorlevel 1 (popd & goto :failed)

echo [7/8] 构建前端...
call pnpm build
if errorlevel 1 (popd & goto :failed)
popd

echo [8/8] 初始化或升级数据库...
"backend\.venv\Scripts\python.exe" -c "from app.config import Settings; Settings().data_dir.mkdir(parents=True, exist_ok=True)"
if errorlevel 1 goto :failed
"backend\.venv\Scripts\python.exe" -m alembic -c backend\alembic.ini upgrade head
if errorlevel 1 goto :failed

echo 安装完成。首次 OCR 会在本机初始化或下载所需模型；正常使用不会上传截图。
if not defined QUESTION_PRACTICE_NONINTERACTIVE pause
exit /b 0

:check_ok
echo 基础运行时检查通过。
exit /b 0

:missing_python
echo [错误] 未找到 Python。请安装 Python 3.11，并勾选 Add Python to PATH。
goto :failed

:unsupported_python
echo [错误] 需要 Python 3.11 或 3.12，当前版本不受支持。
goto :failed

:missing_node
echo [错误] 未找到 Node.js。请安装受支持的 Node.js LTS。
goto :failed

:missing_pnpm
echo [错误] 未找到 pnpm。请先运行 corepack enable，然后重试。
goto :failed

:failed
echo 安装未完成。请根据上方错误修复环境后重新运行 install.bat。
if /I not "%~1"=="--check" if not defined QUESTION_PRACTICE_NONINTERACTIVE pause
exit /b 1

