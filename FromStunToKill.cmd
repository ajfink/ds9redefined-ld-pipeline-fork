@echo off
rem FromStunToKill launcher for Windows: double-click, or run from a prompt.
rem   FromStunToKill.cmd            start the app (first run on a new computer sets everything up)
rem   FromStunToKill.cmd setup      run the setup wizard again
rem   FromStunToKill.cmd check      read-only readiness report
rem   FromStunToKill.cmd cli ...    run an fstk command line
rem Works from a mapped drive or a shared folder; nothing is written into this folder.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\launch.ps1" %*
if errorlevel 1 pause
