@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Missing .venv\Scripts\python.exe
  echo Run install first in %~dp0
  pause
  exit /b 1
)
set "PATH=%~dp0.venv\Scripts;%PATH%"
if not exist "%~dp0sche_tasks" mkdir "%~dp0sche_tasks"
if not exist "%~dp0sche_tasks\done" mkdir "%~dp0sche_tasks\done"
start "GenericAgent Scheduler" "%~dp0.venv\Scripts\python.exe" "%~dp0agentmain.py" --reflect "%~dp0reflect\scheduler.py"
"%~dp0.venv\Scripts\python.exe" "%~dp0frontends\tuiapp_v2.py"
pause
