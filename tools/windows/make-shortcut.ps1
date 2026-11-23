<#
  Puts a "FromStunToKill" shortcut on your Desktop and in the Start menu (safe to re-run).
  powershell -NoProfile -ExecutionPolicy Bypass -File Z:\FromStunToKill\tools\windows\make-shortcut.ps1
  Run it from a normal (non-admin) PowerShell so the shortcuts are yours and see your mapped drives.
#>
param([string]$Venv = "$env:LOCALAPPDATA\fstk\venv", [string]$Repo = "Z:\FromStunToKill")
$ErrorActionPreference = "Stop"
$target = "$Venv\Scripts\pythonw.exe"
if (-not (Test-Path $target)) { throw "fstk venv not found at $Venv - run setup-fstk.ps1 first" }
$sh = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath("Desktop"),
                   (Join-Path ([Environment]::GetFolderPath("Programs")) ""))) {
    $lnk = $sh.CreateShortcut((Join-Path $dir "FromStunToKill.lnk"))
    $lnk.TargetPath = $target
    $lnk.Arguments = "-m fstk.ui.app"
    $lnk.WorkingDirectory = "$env:USERPROFILE\FromStunToKill"
    $lnk.Description = "FromStunToKill - LaserDisc to video"
    $lnk.Save()
    Write-Host "created $dir\FromStunToKill.lnk"
}
