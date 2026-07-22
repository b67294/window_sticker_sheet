param([switch]$NoOpen)

$ErrorActionPreference = "Stop"
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppUrl = "http://127.0.0.1:8790"

try {
    $health = Invoke-RestMethod -Uri "$AppUrl/api/health" -Method Get -TimeoutSec 2
    if ($health.ok -eq $true) {
        Write-Host "Window Sticker Sheet Workbench 已经在运行：$AppUrl" -ForegroundColor Green
        if (-not $NoOpen) {
            Start-Process $AppUrl
        }
        exit 0
    }
} catch {
    # No healthy workbench is listening; continue with normal startup.
}

$Python = "C:\Users\melonedoe\miniconda3\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    $Python = "python"
}
Set-Location -LiteralPath $AppDir
Write-Host "正在启动 Window Sticker Sheet Workbench：$AppUrl" -ForegroundColor Cyan
& $Python -m uvicorn app:app --host 127.0.0.1 --port 8790
