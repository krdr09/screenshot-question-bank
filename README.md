# 截图题库 OCR 与刷题系统

这是一个 Windows 本地、单用户、离线优先的题库工具。它把连续截图导入本机，经过 OCR、题目解析、人工复核后形成可编辑题库，并提供刷题、自动判分、错题本和完整备份恢复。截图与业务数据不会上传到网络；项目不包含在线 AI、付费 API、遥测或云服务。

本项目已有阶段 10 自动化验收和阶段 11 固定真实截图验收记录。阶段 11 在单一 Windows/CPU 环境中，使用 22 张固定截图，经人工复核后完成题库与练习流程；该结论不代表任意新截图都能自动无误入库。整理后的历史范围和结果见 [阶段 10 验收摘要](docs/PHASE10_ACCEPTANCE.md) 与 [阶段 11 验收摘要](docs/PHASE11_ACCEPTANCE.md)。

本仓库只包含源码、测试和必要说明，不包含真实截图、题库、备份或原本地提交历史。具体边界见 [仓库范围与隐私](docs/REPOSITORY_PRIVACY.md)。

## Windows 安装与启动

### 前置条件

- Windows 10/11 x64
- Python 3.11 或 3.12，并已加入 `PATH`
- Node.js 和 Corepack
- pnpm 11.7.0（可用 `corepack enable` 后安装）
- 默认浏览器；阶段 10 E2E 默认使用已安装的 Microsoft Edge
- 首次安装 PaddleOCR 模型时需要网络。模型下载完成后，正常导入、识别、刷题和备份均在本机运行

### 安装

双击 `install.bat`，或在项目根目录执行：

```powershell
cmd.exe /d /c install.bat
```

安装脚本会检查运行时、创建 `backend\.venv`、安装固定版本依赖、真实初始化 PaddleOCR CPU 并识别程序生成图片、安装前端依赖、生成生产构建，最后执行数据库迁移。任一步失败都会返回非零退出码，不会显示伪成功。

只检查脚本所需的基础运行时：

```powershell
cmd.exe /d /c install.bat --check
```

### 启动

双击 `start.bat`，或执行：

```powershell
cmd.exe /d /c start.bat
```

脚本先执行数据库迁移，再把 FastAPI 绑定到 `127.0.0.1:8765`，健康检查成功后自动打开首页。不会绑定 `0.0.0.0`。可在启动前指定回环端口：

```powershell
$env:QUESTION_PRACTICE_PORT = "8877"
cmd.exe /d /c start.bat
```

启动前检查可用：

```powershell
cmd.exe /d /c start.bat --check
```

## 本机数据

默认数据位于项目根目录的 `data\`：

- `data\app.db`：SQLite 题库、审核、练习和错题状态
- `data\images\`：复制到应用内部的原图与裁切图
- `data\backups\`：应用创建的备份
- `data\logs\`：应用与启动日志

导入只读取用户选择的目录并复制受支持图片，不修改原始截图。请不要把 `data\` 提交到版本控制。需要迁移到另一台电脑时，优先使用应用中的完整备份与恢复，不要在服务运行时直接复制 SQLite 主文件。

## 使用流程

1. 在“本地导入”选择截图文件夹，通过 UI 创建或选择课程、分类和批次。
2. 等待真实后台任务完成；Mock OCR 只用于自动化测试，正常安装默认使用 PaddleOCR。
3. 有顺序冲突、跨截图关系、缺答案或低置信度时，在“待检查中心”查看原图、裁切图与 OCR，修改后确认入库。
4. 在“题库管理”筛选、搜索和编辑正式题目；软删除题目或删除批次前需确认。
5. 在“刷题”按课程、分类、批次和题型创建练习。答案由后端判分；判断题显示“正确/错误”，不会用 A/B 冒充。
6. 在“错题本”查看累计错误、标记掌握或重新练习；“练习历史”保留已提交及进行中记录。

## 备份与恢复

在“备份与恢复”中创建完整 ZIP 后，等待浏览器下载完成并妥善保存。恢复前先选择 ZIP 并运行正式哈希验证；确认恢复会替换当前题库，应用会先创建安全备份。页面显示“需要重启”后必须真正停止并重新启动服务，不能继续使用恢复前的页面内存状态。

完整备份覆盖数据库、原始图片、裁切图片及其关系。恢复后建议从 UI 核对分类、批次、题目、图片、练习历史和错题本。备份 ZIP 仍属于私人数据，请自行加密存储或限制访问。

## OCR 与设备

默认配置：

```powershell
$env:OCR_ENGINE = "paddle"
$env:OCR_DEVICE = "auto"   # auto、cpu 或 gpu
```

`auto` 会检查 PaddlePaddle 是否具备 CUDA，并做真实小型推理；GPU 初始化或运行失败时回退 CPU。`install.bat` 固定安装 `paddlepaddle==3.2.0` CPU 包和 `paddleocr==3.5.0`。GPU 用户需要自行按 PaddlePaddle 官方说明安装与本机 CUDA 匹配的 GPU 包；脚本不会修改显卡驱动或 CUDA。

自动化测试使用 `OCR_ENGINE=mock` 和合成截图，只替换 OCR 引擎本身，导入、队列、数据库、裁切、审核、题库、判分与备份均运行真实实现。

## 开发与常规测试

后端迁移和测试：

```powershell
backend\.venv\Scripts\python.exe -m alembic -c backend\alembic.ini upgrade head
cd backend
.venv\Scripts\python.exe -m pytest -q
```

前端测试与生产构建：

```powershell
pnpm --dir frontend test
pnpm --dir frontend build
```

开发服务器仅用于开发，不用于阶段 10 E2E 验收：

```powershell
cd frontend
pnpm dev
```

## Playwright 真实生产栈验收

首次准备 E2E 依赖：

```powershell
cd e2e
pnpm install --frozen-lockfile
```

Windows 默认通过 Playwright `msedge` channel 使用已安装的 Microsoft Edge：

```powershell
$env:E2E_BROWSER = "edge"
$env:E2E_PYTHON = (Resolve-Path ..\backend\.venv\Scripts\python.exe)
```

若选择 Playwright Chromium，必须先安装浏览器：

```powershell
pnpm exec playwright install chromium
$env:E2E_BROWSER = "chromium"
```

`E2E_BROWSER=edge` 且 Edge 不存在时测试会明确失败，不会静默回退。测试代码不依赖 Edge 独有行为，Chromium 也应通过。

快速回归只运行 A–D，完整验收运行 A–E；二者共享完全相同的生产构建、FastAPI、worker、隔离目录和进程管理运行器：

```powershell
pnpm test:quick
pnpm test:full
pnpm test:scenario -- --scenario A
pnpm test:support
```

每个业务场景可将 `A` 改为 `B`、`C`、`D` 或 `E` 单独调试。场景开始前创建独立数据库、数据、日志、备份、OCR、上传、配置、任务状态和端口；主要业务操作均经 UI 完成。等待使用条件轮询与明确超时，不使用固定长时间 sleep。

失败证据保存在 `e2e\artifacts\<run-id>\<scenario>\`，包括 trace、最终截图、HTML、控制台、pageerror、失败请求、FastAPI/应用日志、停止后的 SQLite 主库及 WAL/SHM（或一致快照）、配置摘要和浏览器版本。文本证据会扫描并脱敏用户目录、工作区/临时绝对路径、令牌、密钥、Cookie、Authorization 和非合成真实文件名。证据复制校验失败时会保留原始临时目录。

## Windows 脚本验收

只检查版本控制清单和安全约束：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\verify-windows.ps1 -Mode Check
```

完整模式使用 `git ls-files` 逐文件创建隔离副本，真实运行安装、CPU OCR、生产构建、迁移和 `start.bat`，通过唯一 `startup_probe` 访问日志证明浏览器自动打开首页，并检查 SPA fallback、未知 API 404、精确 PID/启动时间进程树清理、健康不可达和端口释放：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\verify-windows.ps1 -Mode Full
```

隔离副本不包含未提交改动；若工作区不干净，报告会明确说明。成功运行会删除临时副本；失败运行保留在 `.acceptance-runs\<run-id>\`。该命令验证的是当前 Windows 机器上的隔离项目副本，不等同于在另一台全新 Windows 电脑上双击验证。

## 安全边界与已知限制

- 服务只允许 `127.0.0.1`，没有账户系统，也不适合暴露到局域网或公网。
- 不上传原图，不接入在线 AI、云同步、遥测或付费 API。
- 不修改或删除用户原始截图；应用清理只操作已验证的内部受控路径。
- 备份恢复、数据库迁移和后台任务均有自动化测试，但仍应保留独立备份。
- Windows 脚本会在阶段 10 最终验收中记录实际命令和结果；没有实际执行的环境或浏览器组合不会被声称已验证。
- 阶段 11 只验证了上述 22 张固定截图、单一 Windows/CPU/PaddleOCR 环境。最终人工复核后的题库和练习可用，但未经修改的自动结果仍需要人工检查；不得据此推断其他课程、字体、分辨率、截图比例、设备或 GPU 的准确率与适配情况。
