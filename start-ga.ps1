#Requires -Version 7.0
<#
.SYNOPSIS
    GenericAgent TUI v2 launcher — PowerShell 7 + Windows Terminal
.DESCRIPTION
    Starts scheduler (background) + TUI (foreground).
    Usage: pwsh -File start-ga.ps1
           or double-click start-ga-wt.bat
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = $PSScriptRoot
Set-Location $Root

# --- preflight checks ---
$venvPy = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Write-Host "[ERROR] Missing $venvPy" -ForegroundColor Red
    Write-Host "Run install first in $Root"
    Read-Host "Press Enter to exit"
    exit 1
}

# ensure PATH includes venv\Scripts (for any child process)
$env:PATH = "$Root\.venv\Scripts;$env:PATH"

# --- create dirs ---
@('sche_tasks', 'sche_tasks\done') | ForEach-Object {
    $d = Join-Path $Root $_
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d | Out-Null }
}

# --- start scheduler in background ---
$schedulerPy = Join-Path $Root 'agentmain.py'
$reflectDir  = Join-Path $Root 'reflect\scheduler.py'
Write-Host "[GA] Starting scheduler..." -ForegroundColor Cyan
Start-Process -FilePath $venvPy `
    -ArgumentList "`"$schedulerPy`" --reflect `"$reflectDir`"" `
    -WindowStyle Hidden

# --- launch TUI v2 in foreground ---
$tuiPy = Join-Path $Root 'frontends\tuiapp_v2.py'
Write-Host "[GA] Launching TUI v2 ..." -ForegroundColor Green
& $venvPy $tuiPy
