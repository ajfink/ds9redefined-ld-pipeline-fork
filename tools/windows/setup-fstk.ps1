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
    # Where fstk writes: state + working media. Default: your user folder on this machine's own disk
    # (C:\Users\<you>\FromStunToKill). Captures (X:) and the DVD (Z:\Video) are only ever read.
    [string]$DataRoot  = "$env:USERPROFILE\FromStunToKill\data",
    [string]$MediaRoot = "$env:USERPROFILE\FromStunToKill\work",
    [string]$OutRoot   = "",
    [string]$DvdRoot   = "Z:\Video",
    [string]$Downloads = "X:\",
    [switch]$Force
)
$ErrorActionPreference = "Stop"
function Step($t) { Write-Host "`n== $t" -ForegroundColor Cyan }

Step "Python 3.12"
# Returns the full path of a real CPython 3.12 (not conda's), or $null. Looks past PATH because a fresh winget
# install is not on this session's PATH yet.
function Find-Python312 {
    $env:Path = $env:Path + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    $cands = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        try { $x = & py -3.12 -c "import sys; print(sys.executable)" 2>$null
              if ($LASTEXITCODE -eq 0 -and $x) { $cands += $x } } catch {}
    }
    $cands += "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
              "$env:ProgramFiles\Python312\python.exe", "${env:ProgramFiles(x86)}\Python312\python.exe"
    foreach ($c in $cands) {
        if ($c -and (Test-Path $c)) {
            try { $v = & $c -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null } catch { $v = "" }
            if ($v -eq "3.12") { return $c }
        }
    }
    return $null
}
$py = Find-Python312
if (-not $py) {
    Write-Host "Python 3.12 not found; installing with winget"
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    $py = Find-Python312
}
if (-not $py) { throw "Python 3.12 is installed but could not be located; open a new PowerShell window and re-run this script" }
Write-Host "using $py"

Step "venv at $Venv"
if (-not (Test-Path "$Venv\Scripts\python.exe")) { & $py -m venv $Venv; if ($LASTEXITCODE -ne 0) { throw "venv creation failed" } }
$vpy = "$Venv\Scripts\python.exe"
& $vpy -m pip install --upgrade pip
& $vpy -m pip install -e "$Repo[gui,media]"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Step "VapourSynth plugins load"
& $vpy -c "import vapoursynth as vs; c=vs.core; print('VapourSynth', c.core_version.release_major, sorted(p.namespace for p in c.plugins()))"

Step "profile"
$mp = @("$Repo\tools\windows\make_profile.py", "--v38", $V38, "--data-root", $DataRoot, "--media-root", $MediaRoot,
          "--dvd-root", $DvdRoot, "--downloads-root", $Downloads)
if ($OutRoot) { $mp += @("--out-root", $OutRoot) }
if ($Force) { $mp += "--force" }
& $vpy @mp
if ($LASTEXITCODE -eq 1) { Write-Host "keeping the existing profile (pass -Force to rewrite it)" }
elseif ($LASTEXITCODE -ne 0) { throw "profile step failed (see above)" }

Step "disk space where fstk writes"
foreach ($d in @($DataRoot, $MediaRoot)) {
    $q = Split-Path -Qualifier $d
    $free = (Get-PSDrive ($q.TrimEnd(':'))).Free / 1GB
    Write-Host ("{0,-50} {1,8:N0} GB free on {2}" -f $d, $free, $q)
}
Write-Host "An episode needs about 60 GB of working space (master ~40 GB, audio, two encodes) plus the 80 GB reserve."
Write-Host "Change the folders any time: fstk profile folders --media-root D:\somewhere   (or Folders… in the app)"

Step "doctor"
& "$Venv\Scripts\fstk.exe" doctor
Write-Host "`nfstk is in $Venv\Scripts (fstk.exe, fstk-gui.exe). Next: docs\WINDOWS-RUN.md" -ForegroundColor Green

Step "desktop shortcut"
try { & "$Repo\tools\windows\make-shortcut.ps1" -Venv $Venv -Repo $Repo }
catch { Write-Host "shortcut not created: $_ (run tools\windows\make-shortcut.ps1 yourself)" -ForegroundColor Yellow }
