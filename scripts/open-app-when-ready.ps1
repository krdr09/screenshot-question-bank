[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Url,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$OpenUrl,

    [ValidateNotNullOrEmpty()]
    [string]$UrlHandler = "rundll32.exe",

    [ValidateRange(1, 1000)]
    [int]$MaxAttempts = 60,

    [ValidateRange(0, 60000)]
    [int]$RetryDelayMilliseconds = 500
)

$healthUrl = $Url.TrimEnd('/') + "/api/health"
$healthy = $false

for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
    try {
        Invoke-WebRequest -UseBasicParsing $healthUrl -TimeoutSec 1 | Out-Null
        $healthy = $true
        break
    }
    catch {
        if ($attempt -lt $MaxAttempts) {
            Start-Sleep -Milliseconds $RetryDelayMilliseconds
        }
    }
}

if (-not $healthy) {
    exit 1
}

& $UrlHandler "url.dll,FileProtocolHandler" $OpenUrl
exit 0
