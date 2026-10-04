@echo off
:: GenericAgent TUI v2 — launch in Windows Terminal + PowerShell 7
cd /d "%~dp0"
start "" wt.exe pwsh.exe -ExecutionPolicy Bypass -NoExit -File "%~dp0start-ga.ps1"
