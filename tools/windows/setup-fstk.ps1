<#
  One-time setup of fstk on this Windows machine (safe to re-run).

  powershell -NoProfile -ExecutionPolicy Bypass -File Z:\FromStunToKill\tools\windows\setup-fstk.ps1

  1. Python 3.12 (py launcher, else winget)      3. this machine's profile from Z:\ld-pipeline\machine.json
  2. a venv outside Dropbox/Z: with fstk + GUI   4. fstk doctor (read-only readiness report)
     + VapourSynth and all plugins (PyPI wheels)
#>
param(
    [string]$Repo      = "Z:\FromStunToKill",
    [string]$Venv      = "$env:LOCALAPPDATA\fstk\venv",
    [string]$V38       = "Z:\ld-pipeline\machine.json",
    [string]$DataRoot  = "Z:\fstk-data",
    [string]$MediaRoot = "X:\fstk-media",
    [string]$DvdRoot   = "Z:\Video",
    [string]$Downloads = "X:\",
    [switch]$Force
)
$ErrorActionPreference = "Stop"
function Step($t) { Write-Host "`n== $t" -ForegroundColor Cyan }

Step "Python 3.12"
$py = $null
try { & py -3.12 -c "import sys; print(sys.version)"; if ($LASTEXITCODE -eq 0) { $py = "py" } } catch {}
if (-not $py) {
    Write-Host "Python 3.12 not found via the py launcher; installing with winget"
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    $py = "py"
}

Step "venv at $Venv"
if (-not (Test-Path "$Venv\Scripts\python.exe")) { & $py -3.12 -m venv $Venv }
$vpy = "$Venv\Scripts\python.exe"
& $vpy -m pip install --upgrade pip
& $vpy -m pip install -e "$Repo[gui,media]"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Step "VapourSynth plugins load"
& $vpy -c "import vapoursynth as vs; c=vs.core; print('VapourSynth', c.core_version.release_major, sorted(p.namespace for p in c.plugins()))"

Step "profile"
$mp = @("$Repo\tools\windows\make_profile.py", "--v38", $V38, "--data-root", $DataRoot, "--media-root", $MediaRoot,
          "--dvd-root", $DvdRoot, "--downloads-root", $Downloads)
if ($Force) { $mp += "--force" }
& $vpy @mp
if ($LASTEXITCODE -eq 1) { Write-Host "keeping the existing profile (pass -Force to rewrite it)" }
elseif ($LASTEXITCODE -ne 0) { throw "profile step failed (see above)" }

Step "doctor"
& "$Venv\Scripts\fstk.exe" doctor
Write-Host "`nfstk is in $Venv\Scripts (fstk.exe, fstk-gui.exe). Next: docs\WINDOWS-RUN.md" -ForegroundColor Green
