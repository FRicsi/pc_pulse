@echo off
cd /d "%~dp0"
if /I "%~1"=="api" (
  powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0windows\Control-Background.ps1" -Action Restart -Json
  exit /b
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\Control-Background.ps1" -Action Restart
pause
