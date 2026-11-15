# Motion probe: mean |frame - previous frame| in luma, on a downscaled clip.
# Cheap, and independent of TFM's cadence decisions.  Used to tell a static
# telecined shot (no motion, all 'c') from a native-30p shot (real motion, all 'c').
#
# ffmpeg's filtergraph parser eats the ':' in a Windows path, so the raw log is
# written with a bare filename while the process runs in the output directory,
# then streamed into the final one-value-per-frame file.
#
# The scale BEFORE tblend is load-bearing: differencing at full resolution leaves
# per-pixel noise dominating the mean (~4.5 on the E04 regression clip vs the
# expected ~2.5).  Regression target: Z:\work\probe\motion-s01e04.txt.
param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Out,
    [int]$ScaleW    = 384,
    [int]$Step      = 1,        # analyse every Nth source frame (1 = all)
    [int]$Frames    = 0         # 0 = whole clip
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'pipeline.ps1')
$prof = Get-LdProfile
$ff = Get-LdTool $prof 'ffmpeg'
Assert-LdWritablePath -Profile $prof -Path $Out

$dir  = Split-Path -Parent $Out
if (-not $dir) { $dir = '.' }
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$raw  = Join-Path $dir 'motion-raw.txt'
Assert-LdWritablePath -Profile $prof -Path $raw
if (Test-Path $raw) { Remove-Item $raw -Force }

$vf = "scale=${ScaleW}:-2:flags=lanczos"
if ($Step -gt 1) { $vf += ",select='not(mod(n\,$Step))'" }
$vf += ",tblend=all_mode=difference,signalstats,metadata=print:file=motion-raw.txt"

$in = @()
if ([System.IO.Path]::GetExtension($Source) -eq '.avs') { $in = @('-f', 'avisynth') }
$cnt = @()
if ($Frames -gt 0) { $cnt = @('-frames:v', "$Frames") }

$sw = [Diagnostics.Stopwatch]::StartNew()
Push-Location $dir
try {
    $eap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $ff -hide_banner -v error @in -i $Source -vf $vf @cnt -f null - 2>&1 | Out-String | Write-Host
    $ErrorActionPreference = $eap
} finally {
    Pop-Location
}
$sw.Stop()
"ffmpeg: $([math]::Round($sw.Elapsed.TotalSeconds,1)) s"
if (-not (Test-Path $raw)) { throw "ffmpeg produced no metadata log ($raw)" }

$vals = New-Object 'System.Collections.Generic.List[double]'
foreach ($line in [System.IO.File]::ReadLines($raw)) {
    if ($line -match 'lavfi\.signalstats\.YAVG=([\d.]+)') { $vals.Add([double]$Matches[1]) }
}
if ($vals.Count -eq 0) { throw "no YAVG values captured from $Source" }
if ($Step -gt 1) {
    $exp = New-Object 'System.Collections.Generic.List[double]'
    foreach ($v in $vals) { for ($k = 0; $k -lt $Step; $k++) { $exp.Add($v) } }
    $vals = $exp
}
[System.IO.File]::WriteAllLines($Out, ($vals | ForEach-Object { '{0:N4}' -f $_ }))
Remove-Item $raw -Force
"frames: $($vals.Count)  ->  $Out"
