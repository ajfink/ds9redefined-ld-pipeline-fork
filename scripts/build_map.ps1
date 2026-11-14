# Build the final 23.976 chain from a full-side TFM log.
#
# Input : a TFM(order=-1, pp=0, chroma=true, slow=2, output=<Log>) decision log
#         optionally a motion trace from motion-probe.ps1 (mean |frame-prev| per frame)
# Output: cadence stats, the deinterlace replacement map, and a ready AviSynth
#         script with that map baked in.
param(
    [string]$Episode = '',
    [string]$Log = '',
    [string]$Motion = '',
    [string]$Source = '',
    [string]$MapOut = '',
    [string]$SumOut = '',
    [string]$DiOut = '',
    [string]$CadOut = '',
    [int]$SourcePad = -1,
    [int]$TrimStart = -1,
    [int]$TrimEnd = -1,
    [switch]$DryRun,
    [switch]$Force,

    # mic > MI reproduces TFM's own '+' flag exactly (MI = TFM's default).
    [int]$MI = 80,
    [int]$RepairMI = 65,
    [int]$WinLen = 90,
    [int]$WinStep = 15,
    [int]$WinComb = 40
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'pipeline.ps1')
$prof = Get-LdProfile
if ($Episode) {
    $ep = Get-LdEpisode -Code $Episode
    if (-not $Log) { $Log = Resolve-LdFile $prof $ep 'tfm_log' }
    if (-not $Source) { $Source = Resolve-LdFile $prof $ep 'stack_master' }
    if (-not $MapOut) { $MapOut = Resolve-LdFile $prof $ep 'avs_encode' }
    if (-not $Motion) { $Motion = Resolve-LdFile $prof $ep 'motion' -Optional }
    if (-not $SumOut) { $SumOut = Resolve-LdFile $prof $ep 'cadence_summary' -Optional }
    if (-not $DiOut) { $DiOut = Resolve-LdFile $prof $ep 'di_map' -Optional }
    if ($SourcePad -lt 0) {
        if ($null -eq $ep.audio.pad_frames) { throw "$Episode audio.pad_frames is not measured." }
        $SourcePad = [int]$ep.audio.pad_frames
    }
    if ($TrimStart -lt 0) {
        if ($null -eq $ep.video.head_trim) { throw "$Episode video.head_trim is not measured." }
        $TrimStart = [int]$ep.video.head_trim
    }
    if ($TrimEnd -lt 0) {
        if ($null -eq $ep.video.tail_trim) { throw "$Episode video.tail_trim is not measured; record 0 only if no tail trim was measured." }
        $TrimEnd = [int]$ep.video.tail_trim
    }
}
if (-not $Log) { throw 'Supply -Log with the current episode TFM log.' }
if (-not $Source) { throw 'Supply -Source with the current episode stack master.' }
if (-not $MapOut) { throw 'Supply -MapOut with the generated AVS output path.' }
if ($SourcePad -lt 0 -or $TrimStart -lt 0 -or $TrimEnd -lt 0) {
    throw 'Supply measured -SourcePad, -TrimStart, and -TrimEnd, or use -Episode with those values in the manifest.'
}
$probeRoot = Get-LdRoot $prof 'probe'
if (-not $SumOut) { $SumOut = Join-Path $probeRoot (([IO.Path]::GetFileNameWithoutExtension($Log)) + '-cadence-summary.txt') }
if (-not $DiOut) { $DiOut = Join-Path $probeRoot (([IO.Path]::GetFileNameWithoutExtension($Log)) + '-di-map.txt') }
if (-not $CadOut) { $CadOut = Join-Path $probeRoot (([IO.Path]::GetFileNameWithoutExtension($Log)) + '-full-cadence.txt') }
foreach ($f in @($Log, $Source)) { if (-not (Test-Path -LiteralPath $f)) { throw "Input not found: $f" } }
foreach ($f in @($MapOut, $SumOut, $DiOut, $CadOut)) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $f) | Out-Null
}
$existingOutputs = @($MapOut, $SumOut, $DiOut, $CadOut) | Where-Object { Test-Path -LiteralPath $_ }
if ($existingOutputs -and -not $Force -and -not $DryRun) {
    throw "Refusing to overwrite existing cadence artifacts: $($existingOutputs -join ', '). Use -Force only after preserving them."
}

# Two log formats exist:
#   pp > 0 : "<f> <code> <flag> [<mic>]"   flag is '+' when the frame is still combed
#   pp = 0 : "<f> <code> [<mic>]"          no flag column at all
# TFM writes the flag only when pp > 0, but it always writes <mic>, and the flag is
# exactly (mic > MI) with MI = 80.  Verified on 5,400 frames: 266/266 true positives,
# 0 false positives, 0 misses.  So derive it when the column is absent.
$frame = New-Object 'System.Collections.Generic.List[int]'
$code  = New-Object 'System.Collections.Generic.List[string]'
$flag  = New-Object 'System.Collections.Generic.List[char]'
$mic   = New-Object 'System.Collections.Generic.List[double]'
$hadFlag = 0

foreach ($line in [System.IO.File]::ReadLines($Log)) {
    if ($line -match '^(\d+)\s+([a-z])\s+([-+])\s+\[(\d+)\]') {
        $frame.Add([int]$Matches[1]); $code.Add($Matches[2])
        $flag.Add([char]$Matches[3]); $mic.Add([double]$Matches[4])
        $hadFlag++
    }
    elseif ($line -match '^(\d+)\s+([a-z])\s+\[(\d+)\]') {
        $frame.Add([int]$Matches[1]); $code.Add($Matches[2])
        $flag.Add([char]$(if ([double]$Matches[3] -gt $MI) { '+' } else { '-' }))
        $mic.Add([double]$Matches[3])
    }
}
$n = $frame.Count
if ($n -lt 500) { throw "TFM log looks truncated: only $n frames parsed from $Log" }

# optional motion trace
$mot = $null
if ($Motion -and (Test-Path $Motion)) {
    $mot = New-Object 'System.Collections.Generic.List[double]'
    foreach ($l in [System.IO.File]::ReadLines($Motion)) {
        if ($l -match '^\s*([\d.]+)\s*$') { $mot.Add([double]$Matches[1]) }
    }
    if ($mot.Count -lt $n) {
        # tblend emits no value for frame 0, so the probe returns n-1 values for an n-frame clip
        if ($mot.Count -eq $n - 1) {
            $mot.Insert(0, 0.0)
            Write-Host "note: motion trace was short by 1 frame (tblend drops frame 0); padded with 0"
        } else {
            Write-Host "WARNING: motion trace has $($mot.Count) frames, TFM log has $n - motion columns omitted"
            $mot = $null
        }
    }
}

$report = New-Object 'System.Collections.Generic.List[string]'
function Say([string]$s) { $report.Add($s); Write-Host $s }

Say "log    : $Log"
if ($mot) { Say "motion : $Motion ($($mot.Count) frames)" }
Say "frames : $n   first=$($frame[0])  last=$($frame[$n-1])"
if ($hadFlag -eq 0) {
    Say "flag   : log has no flag column (pp=0) - derived as mic > $MI"
}
Say ""
Say "match-code histogram:"
foreach ($g in ($code | Group-Object | Sort-Object Name)) {
    Say ("  {0} : {1,7}  {2,6:P2}" -f $g.Name, $g.Count, ($g.Count / $n))
}
$plus = 0; for ($i = 0; $i -lt $n; $i++) { if ($flag[$i] -eq '+') { $plus++ } }
Say ("  combed ('+') : {0,7}  {1,6:P2}" -f $plus, ($plus / $n))

$p5 = 0
for ($i = 5; $i -lt $n; $i++) { if ($code[$i] -eq $code[$i - 5]) { $p5++ } }
$p5pct = 100.0 * $p5 / ($n - 5)
Say ""
Say ("period-5 fit (code[i] == code[i-5]) : {0:N2} %" -f $p5pct)

$act = 0; for ($i = 0; $i -lt $n; $i++) { if ($code[$i] -ne 'c') { $act++ } }
Say ("telecine activity (code != c)        : {0:N2} %" -f (100.0 * $act / $n))

$u = 0; for ($i = 0; $i -lt $n; $i++) { if ($code[$i] -eq 'u') { $u++ } }
Say ("unmatched ('u')                      : {0,7}  {1,6:P2}" -f $u, ($u / $n))

# ---- 30 second windows -----------------------------------------------------
Say ""
if ($mot) {
    Say "--- 30 s windows: act% / p5% / combed% / mic(mean,peak) / motion(mean,p90) ---"
} else {
    Say "--- 30 s windows: act% / p5% / combed% / mic(mean,peak) ---"
}
$win = 899
$candidates = New-Object 'System.Collections.Generic.List[string]'
for ($s = 0; $s -lt $n; $s += $win) {
    $e = [math]::Min($n - 1, $s + $win - 1)
    $c = 0; $p = 0; $f = 0; $cnt = 0; $uWin = 0; $micSum = 0.0; $micMax = 0.0
    for ($i = $s; $i -le $e; $i++) {
        $cnt++
        if ($code[$i] -ne 'c') { $c++ }
        if ($code[$i] -eq 'u') { $uWin++ }
        if ($flag[$i] -eq '+') { $f++ }
        if ($mic[$i] -gt $micMax) { $micMax = $mic[$i] }
        $micSum += $mic[$i]
        if ($i -ge $s + 5 -and $code[$i] -eq $code[$i - 5]) { $p++ }
    }
    $actPct = 100.0 * $c / $cnt
    $line = "{0,6}-{1,6}s   act={2,5:N1}%  p5={3,5:N1}%  combed={4,4:N1}%  mic={5,5:N0}/{6,3:N0}" -f `
        [int]($s / 29.97), [int]($e / 29.97), $actPct,
        (100.0 * $p / [math]::Max(1, $cnt - 5)), (100.0 * $f / $cnt), ($micSum / $cnt), $micMax
    $motMean = $null
    if ($mot) {
        $ms = 0.0
        for ($i = $s; $i -le $e; $i++) { $ms += $mot[$i] }
        $motMean = $ms / $cnt
        $mv = New-Object 'System.Collections.Generic.List[double]'
        for ($i = $s; $i -le $e; $i++) { $mv.Add($mot[$i]) }
        $sorted = $mv | Sort-Object
        $p90 = $sorted[[int](0.9 * ($sorted.Count - 1))]
        $line += ("  motion={0,6:N1}/{1,5:N1}" -f $motMean, $p90)
        # gate: real motion but no telecine activity => candidate native 30p / 60i
        if ($motMean -ge 15.0 -and $actPct -le 12.0) {
            $line += '   <== CHECK'
            $candidates.Add(("{0}-{1}s (motion {2:N1}, act {3:N1}%)" -f [int]($s / 29.97), [int]($e / 29.97), $motMean, $actPct))
        }
    }
    if ($uWin -gt 0) { $line += "  u=$uWin" }
    Say $line
}

Say ""
if ($mot) {
    if ($candidates.Count -eq 0) {
        Say "VERDICT: uniform 3:2 telecine across the whole side - no window has motion without telecine activity."
        Say "         Global IVTC is correct; no per-segment handling needed."
    } else {
        Say "VERDICT: $($candidates.Count) window(s) need inspection before encoding:"
        foreach ($c in $candidates) { Say "  $c" }
        Say "         Inspect each flagged interval using the sequential frame-review procedure in the runbook; do not auto-approve it."
    }
} else {
    Say "VERDICT: motion trace not supplied - re-run with -Motion <file> to run the native-30p gate."
}

# ---- hard spots: runs where field matching did not produce a clean frame ----
# pp>0 logs mark these 'h'; pp=0 logs leave them 'c' and only the mic betrays them,
# so use the comb predicate itself (mic > MIC) plus genuinely unmatched frames ('u').
$isCombed = New-Object 'bool[]' $n
for ($i = 0; $i -lt $n; $i++) { $isCombed[$i] = ($mic[$i] -gt $MI) -or ($code[$i] -eq 'u') }
$bad = New-Object 'System.Collections.Generic.List[object]'
$i = 0
while ($i -lt $n) {
    if ($isCombed[$i]) {
        $j = $i
        while ($j + 1 -lt $n -and $isCombed[$j + 1]) { $j++ }
        $bad.Add([pscustomobject]@{ a = $i; b = $j; len = $j - $i + 1 })
        $i = $j + 1
    } else { $i++ }
}
$longBad = @($bad | Where-Object { $_.len -ge 90 })          # >= 3 s
Say ""
Say ("hard spots (combed or 'u' runs): {0} total, {1} of them >= 3 s" -f $bad.Count, $longBad.Count)
if ($bad.Count) {
    Say "--- longest 15 ---"
    foreach ($r in ($bad | Sort-Object len -Descending | Select-Object -First 15)) {
        $mark = if ($r.len -ge 90) { '  <<< inspect' } else { '' }
        Say ("  {0,6}-{1,6}  {2,6:N1}s-{3,6:N1}s  len {4,5}{5}" -f `
            $r.a, $r.b, ($r.a / 29.97), ($r.b / 29.97), $r.len, $mark)
    }
}
if ($longBad.Count -gt 0) {
    Say ""
    Say "NOTE: $($longBad.Count) hard spot(s) >= 3 s where field matching left combing or failed. All of them are in the"
    Say "      repair map (QTGMC), which is the right treatment for cuts, fades, opticals and dropouts."
    Say "      Only a full 60i shot needs different handling - check any run over ~10 s with segment-check.ps1."
}

# ---- interlaced inserts, found by density ---------------------------------
# A 60i / broken-cadence insert makes mic alternate: a run-length scan sees only
# short runs and misses it entirely.  Density sees it.  Slide a 3 s window and
# flag any window where at least WinComb frames are combed.
$winFlags = New-Object 'System.Collections.Generic.List[object]'
for ($s = 0; $s + $WinLen -le $n; $s += $WinStep) {
    $c = 0
    for ($i = $s; $i -lt $s + $WinLen; $i++) { if ($mic[$i] -gt $MI) { $c++ } }
    if ($c -ge $WinComb) { $winFlags.Add([pscustomobject]@{ a = $s; b = $s + $WinLen - 1 }) }
}
$regions = New-Object 'System.Collections.Generic.List[object]'
foreach ($f in $winFlags) {
    if ($regions.Count -gt 0 -and $f.a -le $regions[$regions.Count - 1].b + 1) {
        $r = $regions[$regions.Count - 1]
        if ($f.b -gt $r.b) { $r.b = $f.b }
    } else {
        $regions.Add([pscustomobject]@{ a = $f.a; b = $f.b })
    }
}
$inRegion = New-Object 'bool[]' $n
foreach ($r in $regions) { for ($i = $r.a; $i -le $r.b; $i++) { $inRegion[$i] = $true } }

Say ""
Say ("interlaced inserts (>= {0}/{1} combed in a {2}-frame window): {3}" -f $WinComb, $WinLen, $WinLen, $regions.Count)
if ($regions.Count) {
    Say "    from        to     len      time          combed  density"
    foreach ($r in $regions) {
        $c = 0; for ($i = $r.a; $i -le $r.b; $i++) { if ($mic[$i] -gt $MI) { $c++ } }
        Say ("  {0,6}  {1,6}  {2,5}  {3,8:N1}s  {4,6}  {5,5:P0}" -f `
            $r.a, $r.b, ($r.b - $r.a + 1), ($r.a / 29.97), $c, ($c / ($r.b - $r.a + 1)))
    }
}

# ---- the repair set --------------------------------------------------------
$repair = New-Object 'bool[]' $n
for ($i = 0; $i -lt $n; $i++) {
    $repair[$i] = ($mic[$i] -gt $RepairMI) -or $inRegion[$i] -or ($code[$i] -eq 'u')
}
$nRepair = 0; for ($i = 0; $i -lt $n; $i++) { if ($repair[$i]) { $nRepair++ } }
$nRegion = 0; for ($i = 0; $i -lt $n; $i++) { if ($inRegion[$i]) { $nRegion++ } }
Say ""
Say ("repair set: mic > {0} or inside an insert  ->  {1} frames ({2:P2})" -f $RepairMI, $nRepair, ($nRepair / $n))
Say ("            of which inserts contribute {0}, the wider threshold {1}" -f `
    $nRegion, ($nRepair - $nRegion))

# ---- combed ranges ---------------------------------------------------------
$ranges = New-Object 'System.Collections.Generic.List[string]'
$curStart = -1; $prev = -2
for ($i = 0; $i -lt $n; $i++) {
    if ($repair[$i]) {
        if ($curStart -lt 0) { $curStart = $i }
        elseif ($i -ne $prev + 1) { $ranges.Add("[$curStart $prev]"); $curStart = $i }
        $prev = $i
    }
}
if ($curStart -ge 0) { $ranges.Add("[$curStart $prev]") }
Say ""
Say ("repair ranges: {0}  covering {1} frames ({2:P2})" -f $ranges.Count, $nRepair, ($nRepair / $n))

$lines = New-Object 'System.Collections.Generic.List[string]'
for ($i = 0; $i -lt $n; $i++) { $lines.Add([string]::Join("`t", @($frame[$i], $code[$i], $flag[$i], $mic[$i]))) }
# ---- generated AviSynth script --------------------------------------------
$srcAvs = $Source.Replace('\', '\\')
$avsLoaderLine = ''
if ($prof.tools.avisynth_loader) {
    if (-not (Test-Path -LiteralPath $prof.tools.avisynth_loader -PathType Leaf)) {
        throw "Configured AviSynth loader does not exist: $($prof.tools.avisynth_loader)"
    }
    $loaderAvs = ([string]$prof.tools.avisynth_loader).Replace('\', '\\').Replace('"', '\"')
    $avsLoaderLine = "Import(`"$loaderAvs`")"
}
$hdr = @"
# DS9 LaserDisc measured cadence map -> 23.976p master (GENERATED by build_map.ps1).
#
# Cadence is measured, not guessed.  TFM's per-frame match log for this side gives
# $('{0:N2}' -f $p5pct)% period-5 agreement, $plus frames still combed at TFM's own threshold
# (mic > 80), and $($regions.Count) interlaced inserts found by density.
#
# Chain: field-match -> swap in a QTGMC deinterlace of the ORIGINAL frame for
# $nRepair frames ($('{0:P2}' -f ($nRepair / $n)): mic > $RepairMI, plus every frame inside an
# interlaced insert) -> drop 1 frame in 5 -> 10 bit.
#
# The repair set is wider than TFM's own flag on purpose.  Frames at mic 66-80 sit right on
# the line and encode across it; inside an insert about half the frames read below 80 while
# being genuinely combed.  Measured on the validate encode: the narrow rule left 53 residual
# combed frames, and 44 of them came from this band.
#
# FPSDivisor=2 is mandatory: without it QTGMC bobs to double rate (2 output frames per
# input frame) and ReplaceFramesSimple would pull frames from the wrong instant.
# Fallback if QTGMC fails: replace pp=0 with pp=2 and delete the ReplaceFramesSimple lines.
$avsLoaderLine
src = LSMASHVideoSource(`"$srcAvs`").Trim($SourcePad, 0)
fm  = src.TFM(order=-1, pp=0, chroma=true, slow=2)
di  = src.QTGMC(Preset=`"Slower`", SourceMatch=3, Lossless=2, TR2=2, Sharpness=0.2, FPSDivisor=2)
"@

$body = New-Object 'System.Collections.Generic.List[string]'
$body.Add($hdr)
$chunk = 40
for ($i = 0; $i -lt $ranges.Count; $i += $chunk) {
    $end = [math]::Min($ranges.Count - 1, $i + $chunk - 1)
    $seg = ($ranges[$i..$end] -join ' ')
    $body.Add(('fm  = ReplaceFramesSimple(fm, di, mappings="{0}")' -f $seg))
}
$avsTrimEnd = if ($TrimEnd -gt 0) { -$TrimEnd } else { 0 }
$body.Add("out = fm.TDecimate(mode=1, cycle=5, cycleR=1, chroma=true).Trim($TrimStart, $avsTrimEnd)")
$body.Add('out = out.ConvertBits(10)')
$body.Add('return out')

Say ""
if ($DryRun) {
    Say "DRY RUN: would write $MapOut"
    Say "DRY RUN: would write $DiOut"
    Say "DRY RUN: would write $CadOut"
    Say "DRY RUN: would write $SumOut"
} else {
    [System.IO.File]::WriteAllText($DiOut, ($ranges -join ' '))
    [System.IO.File]::WriteAllLines($CadOut, $lines)
    [System.IO.File]::WriteAllLines($MapOut, $body)
    [System.IO.File]::WriteAllLines($SumOut, $report)
    Say "wrote $MapOut"
    Say "wrote $DiOut"
    Say "wrote $CadOut"
    Say "wrote $SumOut"
}
