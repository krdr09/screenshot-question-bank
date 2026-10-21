[CmdletBinding()]
param(
    [ValidateSet("Check", "Full")]
    [string]$Mode = "Check",
    [switch]$KeepOnFailure
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Assert-ContainedPath {
    param([string]$Root, [string]$Target)
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $targetFull = [IO.Path]::GetFullPath($Target)
    if (-not $targetFull.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝操作受控验收目录之外的路径"
    }
    return $targetFull
}

function Remove-ControlledDirectory {
    param([string]$Root, [string]$Target)
    if (-not (Test-Path -LiteralPath $Target)) { return }
    $resolvedRoot = (Resolve-Path -LiteralPath $Root -ErrorAction Stop).ProviderPath
    $resolvedTarget = (Resolve-Path -LiteralPath $Target -ErrorAction Stop).ProviderPath
    $verified = Assert-ContainedPath -Root $resolvedRoot -Target $resolvedTarget
    $extendedPath = '\\?\' + $verified
    $lastError = $null
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            [IO.Directory]::Delete($extendedPath, $true)
        } catch {
            $lastError = $_
        }
        if (-not (Test-Path -LiteralPath $verified)) { return }
        if ($attempt -lt 5) { Start-Sleep -Milliseconds (100 * $attempt) }
    }
    throw "验收目录清理失败：$verified；$lastError"
}

function Assert-WindowsScriptEncoding {
    param([string]$Path)
    $bytes = [IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -lt 3 -or $bytes[0] -ne 0xEF -or $bytes[1] -ne 0xBB -or $bytes[2] -ne 0xBF) {
        throw "隔离副本脚本缺少 UTF-8 BOM：$Path"
    }
    $text = [Text.Encoding]::UTF8.GetString($bytes, 3, $bytes.Length - 3)
    if ($text -match "(?<!`r)`n") {
        throw "隔离副本脚本不是完整 CRLF：$Path"
    }
}

function Get-TrackedFiles {
    param([string]$Repository)
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = "git.exe"
    $escapedRepository = $Repository.Replace('"', '\"')
    # 使用 git ls-files -z 的 NUL 分隔输出，避免空格或非 ASCII 文件名被拆分。
    $start.Arguments = "-C `"$escapedRepository`" ls-files -z"
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = [Text.UTF8Encoding]::new($false)
    $start.StandardErrorEncoding = [Text.UTF8Encoding]::new($false)
    $start.CreateNoWindow = $true
    $process = [Diagnostics.Process]::Start($start)
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) { throw "git ls-files 失败：$stderr" }
    return @($stdout.Split([char[]]@([char]0), [StringSplitOptions]::RemoveEmptyEntries))
}

function Start-CmdCommand {
    param([string]$Command, [string]$WorkingDirectory)
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $env:ComSpec
    $start.Arguments = "/d /s /c `"$Command`""
    $start.WorkingDirectory = $WorkingDirectory
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    return [Diagnostics.Process]::Start($start)
}

function Checkout-TrackedFiles {
    param(
        [string]$Repository,
        [string]$Prefix,
        [string[]]$Files,
        [string]$ControlDirectory
    )
    $utf8 = [Text.UTF8Encoding]::new($false)
    $memory = [IO.MemoryStream]::new()
    foreach ($relative in $Files) {
        $bytes = $utf8.GetBytes($relative + [char]0)
        $memory.Write($bytes, 0, $bytes.Length)
    }
    $inputPath = Join-Path $ControlDirectory "tracked-files.nul"
    $stdoutPath = Join-Path $ControlDirectory "checkout-index.stdout.log"
    $stderrPath = Join-Path $ControlDirectory "checkout-index.stderr.log"
    [IO.File]::WriteAllBytes($inputPath, $memory.ToArray())
    $memory.Dispose()
    $command = "git.exe -C `"$Repository`" checkout-index --force --stdin -z --prefix=`"$Prefix`" < `"$inputPath`" > `"$stdoutPath`" 2> `"$stderrPath`""
    $process = Start-CmdCommand -Command $command -WorkingDirectory $Repository
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) {
        throw "git checkout-index 失败：$((Get-Content -LiteralPath $stderrPath -Raw))$((Get-Content -LiteralPath $stdoutPath -Raw))"
    }
}

function Get-ProcessIdentity {
    param([int]$Id)
    $process = Get-Process -Id $Id -ErrorAction Stop
    return [pscustomobject]@{
        Pid = $process.Id
        StartTime = $process.StartTime.ToUniversalTime().Ticks
    }
}

function Assert-ExactIdentityCurrent {
    param($Identity)
    $current = Get-ProcessIdentity -Id ([int]$Identity.Pid)
    if ($current.StartTime -ne [long]$Identity.StartTime) {
        throw "PID $($Identity.Pid) 已被复用，拒绝访问其进程树"
    }
    return $current
}

function Get-DescendantIdentities {
    param($RootIdentity)
    [void](Assert-ExactIdentityCurrent -Identity $RootIdentity)
    $all = @(Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId)
    $known = [Collections.Generic.HashSet[int]]::new()
    [void]$known.Add([int]$RootIdentity.Pid)
    $frontier = [Collections.Generic.Queue[object]]::new()
    $frontier.Enqueue($RootIdentity)
    $descendants = [Collections.Generic.List[object]]::new()
    while ($frontier.Count -gt 0) {
        $parentIdentity = $frontier.Dequeue()
        [void](Assert-ExactIdentityCurrent -Identity $parentIdentity)
        foreach ($candidate in $all) {
            $pidValue = [int]$candidate.ProcessId
            if ([int]$candidate.ParentProcessId -ne [int]$parentIdentity.Pid -or $known.Contains($pidValue)) { continue }
            try { $candidateIdentity = Get-ProcessIdentity -Id $pidValue }
            catch { continue }
            if ($candidateIdentity.StartTime -lt $parentIdentity.StartTime) { continue }
            $descendants.Add($candidateIdentity)
            $frontier.Enqueue($candidateIdentity)
            [void]$known.Add($pidValue)
        }
        [void](Assert-ExactIdentityCurrent -Identity $parentIdentity)
    }
    [void](Assert-ExactIdentityCurrent -Identity $RootIdentity)
    return @($descendants)
}

function Stop-ExactIdentity {
    param($Identity)
    try { $process = Get-Process -Id ([int]$Identity.Pid) -ErrorAction Stop }
    catch { return }
    $handle = $null
    try {
        try {
            $handle = $process.SafeHandle
            $startTime = $process.StartTime.ToUniversalTime().Ticks
        } catch { return }
        if ($startTime -ne [long]$Identity.StartTime) {
            throw "PID $($Identity.Pid) 已被复用，拒绝终止"
        }
        try { if ($process.HasExited) { return } }
        catch { return }
        try { $process.Kill() }
        catch {
            try { if ($process.HasExited) { return } }
            catch { return }
            throw
        }
    } finally {
        if ($null -ne $handle) { $handle.Dispose() }
        $process.Dispose()
    }
}

function Stop-ExactProcessTree {
    param($RootIdentity)
    [void](Assert-ExactIdentityCurrent -Identity $RootIdentity)
    $descendants = @(Get-DescendantIdentities -RootIdentity $RootIdentity)
    [void](Assert-ExactIdentityCurrent -Identity $RootIdentity)
    [array]::Reverse($descendants)
    foreach ($identity in $descendants) { Stop-ExactIdentity -Identity $identity }
    Stop-ExactIdentity -Identity $RootIdentity
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        $remaining = @($descendants + $RootIdentity | Where-Object {
            try {
                $current = Get-ProcessIdentity -Id ([int]$_.Pid)
                $current.StartTime -eq [long]$_.StartTime
            } catch { $false }
        })
        if ($remaining.Count -eq 0) { return }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "记录的进程树未完全退出：$($remaining.Pid -join ',')"
}

function Wait-ExactProcessExit {
    param(
        [Diagnostics.Process]$Process,
        $Identity,
        [int]$TimeoutSeconds,
        [string]$Label
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $nextObservation = [DateTime]::MinValue
    do {
        $Process.Refresh()
        if ($Process.HasExited) {
            $Process.WaitForExit()
            foreach ($observed in $script:activeObservedIdentities.Values) {
                try {
                    [void](Assert-ExactIdentityCurrent -Identity $observed)
                    throw "$Label 根进程退出后仍有已观察子进程：$($observed.Pid)"
                } catch {
                    if ($_.Exception.Message -like "$Label 根进程退出后仍有已观察子进程*") { throw }
                }
            }
            return $Process.ExitCode
        }
        if ([DateTime]::UtcNow -ge $nextObservation) {
            try {
                foreach ($observed in @(Get-DescendantIdentities -RootIdentity $Identity)) {
                    $script:activeObservedIdentities["$($observed.Pid):$($observed.StartTime)"] = $observed
                }
            } catch {
                Write-Verbose "$Label 子进程观察暂不可用：$_"
            }
            $nextObservation = [DateTime]::UtcNow.AddSeconds(1)
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "$Label 超过明确超时 ${TimeoutSeconds}s"
}

function Get-FreeLoopbackPort {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try { return ([Net.IPEndPoint]$listener.LocalEndpoint).Port }
    finally { $listener.Stop() }
}

function Wait-HttpStatus {
    param([string]$Url, [int]$Expected, [int]$TimeoutSeconds = 60)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
            if ([int]$response.StatusCode -eq $Expected) { return $response }
        } catch {
            if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq $Expected) {
                return $_.Exception.Response
            }
        }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "等待 HTTP $Expected 超时：$Url"
}

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$gitCommonDirOutput = @(& git -C $repoRoot rev-parse --path-format=absolute --git-common-dir)
if ($LASTEXITCODE -ne 0 -or $gitCommonDirOutput.Count -ne 1) {
    throw "无法解析 Git 公共目录，拒绝创建 Windows 验收运行根"
}
$gitCommonDir = [IO.Path]::GetFullPath($gitCommonDirOutput[0].Trim())
$workspaceRoot = [IO.Path]::GetFullPath((Split-Path -Parent $gitCommonDir))
$acceptanceRoot = Join-Path $workspaceRoot ".acceptance-runs"
$runId = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssfffZ") + "-" + [Guid]::NewGuid().ToString("N").Substring(0, 8)
$runRoot = Join-Path $acceptanceRoot $runId
$copyRoot = Join-Path $runRoot "source"
$reportPath = Join-Path $runRoot "acceptance-report.json"
$serverProcess = $null
$serverIdentity = $null
$port = $null
$succeeded = $false
$script:activeRootIdentity = $null
$script:activeObservedIdentities = @{}
$previousGitIndex = $env:GIT_INDEX_FILE
$previousGitWorkTree = $env:GIT_WORK_TREE
$temporaryGitIndex = Join-Path $runRoot "head.index"

try {
    New-Item -ItemType Directory -Path $copyRoot -Force | Out-Null
    # git status --porcelain 使用真实工作区和真实 index，只用于报告 dirty 状态。
    $dirtyLines = @(& git -C $repoRoot status --porcelain)
    if ($LASTEXITCODE -ne 0) { throw "git status --porcelain 失败" }
    if ($dirtyLines.Count -gt 0) {
        Write-Warning "当前工作区存在未提交改动；隔离副本不包含这些改动。"
    }

    # 临时 index 固定为 HEAD；git ls-files 和 checkout-index 因此都不会读取
    # 已暂存、未暂存或未跟踪的内容，同时保留 .gitattributes 的 Windows EOL 转换。
    $env:GIT_INDEX_FILE = $temporaryGitIndex
    & git -C $repoRoot read-tree HEAD
    if ($LASTEXITCODE -ne 0) { throw "git read-tree HEAD 失败" }
    $tracked = @(Get-TrackedFiles -Repository $repoRoot)
    $requiredTracked = @(
        "install.bat",
        "start.bat",
        "scripts/open-app-when-ready.ps1",
        "backend/pyproject.toml",
        "backend/uvicorn_logging.json",
        "backend/alembic.ini",
        "frontend/package.json",
        "frontend/pnpm-lock.yaml",
        "backend/app/ocr/install_check.py",
        "scripts/verify-windows.ps1",
        ".gitattributes"
    )
    foreach ($required in $requiredTracked) {
        if ($tracked -notcontains $required) { throw "必要运行文件未纳入版本控制：$required" }
    }

    $env:GIT_WORK_TREE = $copyRoot
    $checkoutPrefix = $copyRoot.Replace('\', '/').TrimEnd('/') + '/'
    $destinations = @{}
    foreach ($relative in $tracked) {
        $destination = Assert-ContainedPath -Root $runRoot -Target (Join-Path $copyRoot $relative)
        $parent = Split-Path -Parent $destination
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
        $destinations[$relative] = $destination
    }
    Checkout-TrackedFiles -Repository $repoRoot -Prefix $checkoutPrefix -Files $tracked -ControlDirectory $runRoot
    foreach ($relative in $tracked) {
        if (-not (Test-Path -LiteralPath $destinations[$relative] -PathType Leaf)) {
            throw "从 HEAD 隔离复制 tracked 文件失败：$relative"
        }
    }
    if ($null -eq $previousGitIndex) { Remove-Item Env:GIT_INDEX_FILE -ErrorAction SilentlyContinue }
    else { $env:GIT_INDEX_FILE = $previousGitIndex }
    if ($null -eq $previousGitWorkTree) { Remove-Item Env:GIT_WORK_TREE -ErrorAction SilentlyContinue }
    else { $env:GIT_WORK_TREE = $previousGitWorkTree }
    foreach ($windowsScript in @("install.bat", "start.bat", "scripts/open-app-when-ready.ps1", "scripts/verify-windows.ps1")) {
        Assert-WindowsScriptEncoding -Path (Join-Path $copyRoot $windowsScript)
    }

    if ($Mode -eq "Check") {
        [pscustomobject]@{
            mode = $Mode
            runId = $runId
            trackedFiles = $tracked.Count
            dirtyWorktree = ($dirtyLines.Count -gt 0)
            dirtyFilesExcluded = $dirtyLines.Count
            result = "passed"
        } | ConvertTo-Json | Set-Content -LiteralPath $reportPath -Encoding UTF8
        Write-Host "Windows 验收检查通过：tracked=$($tracked.Count)，未提交改动=$($dirtyLines.Count)"
        $succeeded = $true
        return
    }

    $env:QUESTION_PRACTICE_NONINTERACTIVE = "1"
    $installOut = Join-Path $runRoot "install.stdout.log"
    $installErr = Join-Path $runRoot "install.stderr.log"
    $install = Start-CmdCommand -Command "chcp 65001>nul & call install.bat > `"$installOut`" 2> `"$installErr`"" -WorkingDirectory $copyRoot
    $installIdentity = Get-ProcessIdentity -Id $install.Id
    $installIdentity | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runRoot "install-identity.json") -Encoding UTF8
    $script:activeRootIdentity = $installIdentity
    $script:activeObservedIdentities = @{}
    $installExit = Wait-ExactProcessExit -Process $install -Identity $installIdentity -TimeoutSeconds 1800 -Label "install.bat"
    $script:activeRootIdentity = $null
    $script:activeObservedIdentities = @{}
    if ($installExit -ne 0) { throw "install.bat 实际执行失败，exit=$installExit" }

    $pipCheckOut = Join-Path $runRoot "pip-check.stdout.log"
    $pipCheckErr = Join-Path $runRoot "pip-check.stderr.log"
    $installedPython = Join-Path $copyRoot "backend/.venv/Scripts/python.exe"
    $pipCheck = Start-CmdCommand -Command "`"$installedPython`" -m pip check > `"$pipCheckOut`" 2> `"$pipCheckErr`"" -WorkingDirectory $copyRoot
    $pipCheckIdentity = Get-ProcessIdentity -Id $pipCheck.Id
    $pipCheckIdentity | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runRoot "pip-check-identity.json") -Encoding UTF8
    $script:activeRootIdentity = $pipCheckIdentity
    $script:activeObservedIdentities = @{}
    $pipCheckExit = Wait-ExactProcessExit -Process $pipCheck -Identity $pipCheckIdentity -TimeoutSeconds 120 -Label "pip check"
    $script:activeRootIdentity = $null
    $script:activeObservedIdentities = @{}
    if ($pipCheckExit -ne 0) { throw "pip check 失败，exit=$pipCheckExit" }

    $port = Get-FreeLoopbackPort
    $probe = $runId
    $env:QUESTION_PRACTICE_PORT = [string]$port
    $env:QUESTION_PRACTICE_STARTUP_PROBE = $probe
    $startOut = Join-Path $runRoot "start.stdout.log"
    $startErr = Join-Path $runRoot "start.stderr.log"
    $serverProcess = Start-CmdCommand -Command "chcp 65001>nul & call start.bat > `"$startOut`" 2> `"$startErr`"" -WorkingDirectory $copyRoot
    $serverIdentity = Get-ProcessIdentity -Id $serverProcess.Id
    $script:activeRootIdentity = $serverIdentity
    $script:activeObservedIdentities = @{}
    [pscustomobject]@{ rootPid = $serverIdentity.Pid; startTime = $serverIdentity.StartTime; port = $port } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runRoot "server-identity.json") -Encoding UTF8

    $baseUrl = "http://127.0.0.1:$port"
    $health = Wait-HttpStatus -Url "$baseUrl/api/health" -Expected 200 -TimeoutSeconds 90
    if ($health.Content -notmatch '"status"\s*:\s*"ok"') { throw "健康检查未返回 ok" }
    $serverChildren = @(Get-DescendantIdentities -RootIdentity $serverIdentity)
    foreach ($serverChild in $serverChildren) {
        $script:activeObservedIdentities["$($serverChild.Pid):$($serverChild.StartTime)"] = $serverChild
    }
    [pscustomobject]@{
        rootPid = $serverIdentity.Pid
        startTime = $serverIdentity.StartTime
        port = $port
        children = @($serverChildren | ForEach-Object { @{ pid = $_.Pid; startTime = $_.StartTime } })
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $runRoot "server-identity.json") -Encoding UTF8
    $index = Wait-HttpStatus -Url "$baseUrl/" -Expected 200
    $spa = Wait-HttpStatus -Url "$baseUrl/library/route" -Expected 200
    if ($index.Content -notmatch '<div id="root"></div>' -or $spa.Content -notmatch '<div id="root"></div>') {
        throw "生产静态首页或 SPA fallback 无效"
    }
    [void](Wait-HttpStatus -Url "$baseUrl/api/this-route-must-not-exist" -Expected 404)

    $accessLog = Join-Path $copyRoot "data/logs/app.log"
    $probePattern = [regex]::Escape("/?startup_probe=$probe")
    $probeDeadline = [DateTime]::UtcNow.AddSeconds(60)
    do {
        if (Test-Path -LiteralPath $accessLog) {
            $accessLogContent = Get-Content -LiteralPath $accessLog -Raw
            $probeMatches = [regex]::Matches($accessLogContent, $probePattern)
            if ($probeMatches.Count -gt 1) { throw "启动探针被重复访问：$($probeMatches.Count) 次" }
            if ($probeMatches.Count -eq 1) {
                $probeSeen = $true
                break
            }
        }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $probeDeadline)
    if (-not $probeSeen) { throw "未在访问日志中发现唯一 startup_probe，无法证明 start.bat 自动打开首页" }
    $probeRepeatDeadline = [DateTime]::UtcNow.AddSeconds(2)
    do {
        Start-Sleep -Milliseconds 200
        $accessLogContent = Get-Content -LiteralPath $accessLog -Raw
        $probeMatches = [regex]::Matches($accessLogContent, $probePattern)
        if ($probeMatches.Count -gt 1) { throw "启动探针被重复访问：$($probeMatches.Count) 次" }
    } while ([DateTime]::UtcNow -lt $probeRepeatDeadline)

    Stop-ExactProcessTree -RootIdentity $serverIdentity
    $serverIdentity = $null
    $script:activeRootIdentity = $null
    $script:activeObservedIdentities = @{}
    $portDeadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri "$baseUrl/api/health" -TimeoutSec 1 | Out-Null
            $healthGone = $false
        } catch { $healthGone = $true }
        $probeSocket = [Net.Sockets.TcpClient]::new()
        try {
            $probeSocket.Connect("127.0.0.1", $port)
            $portReleased = $false
        } catch { $portReleased = $true }
        finally { $probeSocket.Dispose() }
        if ($healthGone -and $portReleased) { break }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $portDeadline)
    if (-not $healthGone -or -not $portReleased) { throw "停止后健康检查仍可访问或端口未释放" }

    [pscustomobject]@{
        mode = $Mode
        runId = $runId
        trackedFiles = $tracked.Count
        dirtyWorktree = ($dirtyLines.Count -gt 0)
        dirtyFilesExcluded = $dirtyLines.Count
        port = $port
        startupProbe = $probe
        result = "passed"
    } | ConvertTo-Json | Set-Content -LiteralPath $reportPath -Encoding UTF8
    Write-Host "Windows 完整验收通过：install、CPU OCR、迁移、生产启动、startup_probe、SPA/API 和精确进程清理均通过。"
    $succeeded = $true
} catch {
    $failure = $_.Exception.Message
    [pscustomobject]@{ mode = $Mode; runId = $runId; result = "failed"; error = $failure } |
        ConvertTo-Json | Set-Content -LiteralPath $reportPath -Encoding UTF8
    Write-Error "Windows 验收失败；已保留运行目录 .acceptance-runs/$runId ：$failure"
    exit 1
} finally {
    if ($script:activeRootIdentity) {
        try { Stop-ExactProcessTree -RootIdentity $script:activeRootIdentity } catch { Write-Warning $_ }
    }
    foreach ($observed in @($script:activeObservedIdentities.Values)) {
        try { Stop-ExactProcessTree -RootIdentity $observed } catch { Write-Verbose $_ }
    }
    Remove-Item Env:QUESTION_PRACTICE_PORT -ErrorAction SilentlyContinue
    Remove-Item Env:QUESTION_PRACTICE_STARTUP_PROBE -ErrorAction SilentlyContinue
    Remove-Item Env:QUESTION_PRACTICE_NONINTERACTIVE -ErrorAction SilentlyContinue
    if ($null -eq $previousGitIndex) { Remove-Item Env:GIT_INDEX_FILE -ErrorAction SilentlyContinue }
    else { $env:GIT_INDEX_FILE = $previousGitIndex }
    if ($null -eq $previousGitWorkTree) { Remove-Item Env:GIT_WORK_TREE -ErrorAction SilentlyContinue }
    else { $env:GIT_WORK_TREE = $previousGitWorkTree }
    if ($succeeded -and -not $KeepOnFailure) {
        Remove-ControlledDirectory -Root $acceptanceRoot -Target $runRoot
    }
}
