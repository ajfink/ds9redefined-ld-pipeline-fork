# FromStunToKill launcher for Windows (started by FromStunToKill.cmd).
# Finds Python 3.12-3.15 (or offers to download a private, checksum-verified copy), then hands over to
# tools\fstk_launch.py, which builds this machine's environment, runs setup on a new machine and starts the app.
# Nothing is written into the app folder: everything this script creates lives under %LOCALAPPDATA%\fstk.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Core = Join-Path $PSScriptRoot 'fstk_launch.py'
$Local = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $HOME 'AppData\Local' }
$Machine = Join-Path $Local 'fstk'
$Private = Join-Path $Machine 'python\python.exe'
$Check = 'import sys; sys.exit(0 if (3,12) <= sys.version_info[:2] < (3,16) else 1)'

function Test-Python([string]$exe, [string[]]$prefix) {
    try {
        & $exe @prefix -c $Check 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

function Find-Python {
    foreach ($v in '3.13', '3.12', '3.14', '3.15') {
        if ((Get-Command py -ErrorAction SilentlyContinue) -and (Test-Python 'py' @("-$v"))) {
            return (& py "-$v" -c 'import sys; print(sys.executable)').Trim()
        }
    }
    foreach ($name in 'python', 'python3') {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and ($cmd.Source -notmatch 'WindowsApps') -and (Test-Python $cmd.Source @())) { return $cmd.Source }
    }
    $roots = @("$Local\Programs\Python", $env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { $_ }
    foreach ($root in $roots) {
        foreach ($v in '313', '312', '314', '315') {
            foreach ($rel in "Python$v\python.exe", "Python\Python$v\python.exe") {
                $exe = Join-Path $root $rel
                if ((Test-Path $exe) -and (Test-Python $exe @())) { return $exe }
            }
        }
    }
    if ((Test-Path $Private) -and (Test-Python $Private @())) { return $Private }
    return $null
}

function Install-PrivatePython {
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'aarch64-pc-windows-msvc' } else { 'x86_64-pc-windows-msvc' }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $meta = Invoke-RestMethod 'https://raw.githubusercontent.com/astral-sh/python-build-standalone/latest-release/latest-release.json'
    $tag = $meta.tag
    $prefix = $meta.asset_url_prefix
    $reply = Invoke-WebRequest -UseBasicParsing "$prefix/SHA256SUMS"
    # GitHub serves release files as application/octet-stream, which Windows PowerShell 5.1 hands back as bytes,
    # not text: split those bytes as text and no line ever matched ("no Python 3.12 build ...").
    $text = if ($reply.Content -is [byte[]]) { [Text.Encoding]::UTF8.GetString($reply.Content) } else { [string]$reply.Content }
    $sums = $text -split "`r?`n"
    $pattern = "cpython-3\.12\.\d+\+$tag-$arch-install_only\.tar\.gz$"
    $line = $sums | Where-Object { $_.Trim() -match $pattern } | Select-Object -First 1
    if (-not $line) { throw "no Python 3.12 build for $arch in release $tag (the checksum list had $($sums.Count) lines)" }
    $hash, $file = ($line.Trim() -split '\s+')
    New-Item -ItemType Directory -Force -Path $Machine | Out-Null
    $archive = Join-Path $Machine $file
    Write-Host "Downloading $file ..."
    Invoke-WebRequest -UseBasicParsing "$prefix/$file" -OutFile $archive
    $got = (Get-FileHash $archive -Algorithm SHA256).Hash.ToLower()
    if ($got -ne $hash.ToLower()) { Remove-Item $archive -Force; throw "checksum mismatch for $file" }
    if (Test-Path (Join-Path $Machine 'python')) { Remove-Item (Join-Path $Machine 'python') -Recurse -Force }
    & tar.exe -xzf $archive -C $Machine
    if ($LASTEXITCODE -ne 0) { throw 'could not unpack the Python archive' }
    Remove-Item $archive -Force
    if (-not (Test-Path $Private)) { throw 'the unpacked Python was not where expected' }
}

try {
    $py = Find-Python
    if (-not $py) {
        Write-Host 'FromStunToKill needs Python 3.12 to 3.15, and none was found on this computer.'
        Write-Host 'It can download a private copy (python-build-standalone, checksum-verified, about 40 MB) into'
        Write-Host "  $Machine\python"
        Write-Host 'No administrator rights are needed and nothing else on the computer is changed.'
        $answer = Read-Host 'Download it now? [Y/n]'
        if ($answer -match '^[nN]') {
            Write-Host 'Install Python 3.12 or newer from https://www.python.org/downloads/ and start this again.'
            Read-Host 'Press Enter to close' | Out-Null
            exit 3
        }
        Install-PrivatePython
        $py = $Private
    }
    & $py $Core @args
    exit $LASTEXITCODE
} catch {
    Write-Host ''
    Write-Host "Could not start FromStunToKill: $($_.Exception.Message)" -ForegroundColor Red
    Read-Host 'Press Enter to close' | Out-Null
    exit 1
}
