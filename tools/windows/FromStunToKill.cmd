@echo off
rem Double-click to start FromStunToKill (no console window). Uses the venv that setup-fstk.ps1 made.
set "VENV=%LOCALAPPDATA%\fstk\venv"
if not exist "%VENV%\Scripts\pythonw.exe" (
  echo fstk is not set up yet. Run tools\windows\setup-fstk.ps1 first.
  pause
  exit /b 1
)
start "" "%VENV%\Scripts\pythonw.exe" -m fstk.ui.app
