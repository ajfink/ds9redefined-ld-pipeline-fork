<#
.SYNOPSIS
  Validate the source-derived AVS and make the single final one-part x265 encode.

.DESCRIPTION
  The validate pass probes AVS metadata and decodes a short sample to the null muxer;
  it does not encode video. The final pass requires the source-derived PAD-repair AVS,
  measured frame count and duration; it writes one video-only x265 CRF 14 / slow /
  tune grain / Main 10 encode to padfixed_video. Corrected audio is built independently
  and attached later by the stream-copy remux. It never overwrites an output.

  This generic script intentionally rejects two-part Emissary. E01 requires its
  separate per-part workflow and a separate QC contract.

.EXAMPLE
  powershell -File scripts\encode_episode.ps1 -Episode S01E06 -Pass validate -DryRun
  powershell -File scripts\encode_episode.ps1 -Episode S01E06 -Pass final
#>
param(
    [Parameter(Mandatory)][string]$Episode,
    [ValidateSet('validate', 'final')][string]$Pass = 'final',
    [string]$Output,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'pipeline.ps1')

$prof = Get-LdProfile
$ep = Get-LdEpisode -Code $Episode
if ($ep.format -eq 'two_part' -or $ep.record_status -eq 'measured-partial') {
    throw "$Episode is a two-part/partial record; use its dedicated per-part procedure, not encode_episode.ps1."
}
$ff = Get-LdTool $prof 'ffmpeg'
$fp = Get-LdTool $prof 'ffprobe'
$baseAvs = Resolve-LdFile $prof $ep 'avs_encode'
$padfixAvs = Resolve-LdFile $prof $ep 'avs_padfix'
$avs = if ($Pass -eq 'final') { $padfixAvs } else { $baseAvs }
$stack = Resolve-LdFile $prof $ep 'stack_master'
if (-not (Test-Path -LiteralPath $baseAvs)) { throw "source-derived AviSynth script missing: $baseAvs" }
if (-not (Test-Path -LiteralPath $stack)) { throw "stack master missing: $stack" }
if ($Pass -eq 'final' -and -not (Test-Path -LiteralPath $padfixAvs)) {
    throw "source-derived PAD-repair AVS is required before the one final encode: $padfixAvs; run the PAD audit/replacement/build stages first"
}
if (-not (Test-Path -LiteralPath $avs)) { throw "AviSynth encode script missing: $avs" }
if ($Pass -eq 'final') {
    $workflowVersion = (Get-Content -LiteralPath (Join-Path (Split-Path -Parent $PSScriptRoot) 'WORKFLOW_VERSION' ) -Raw).Trim()
    $padfixText = Get-Content -LiteralPath $padfixAvs -Raw
    $expectedImport = 'Import("' + ([string]$baseAvs).Replace('\', '\\').Replace('"', '\"') + '")'
    if ($padfixText -notmatch "(?m)^# LD_PIPELINE_WORKFLOW_VERSION=$([regex]::Escape($workflowVersion))\s*$" -or
        -not $padfixText.Contains($expectedImport) -or $padfixText -match 'FFVideoSource\s*\(') {
        throw "PAD AVS is stale or does not import the source-derived AVS for workflow Version $workflowVersion. Preserve it, rebuild with build_padfix_avs.py, then validate."
    }
} elseif (Test-Path -LiteralPath $padfixAvs) {
    $workflowVersion = (Get-Content -LiteralPath (Join-Path (Split-Path -Parent $PSScriptRoot) 'WORKFLOW_VERSION') -Raw).Trim()
    $candidateText = Get-Content -LiteralPath $padfixAvs -Raw
    $expectedImport = 'Import("' + ([string]$baseAvs).Replace('\', '\\').Replace('"', '\"') + '")'
    if ($candidateText -match "(?m)^# LD_PIPELINE_WORKFLOW_VERSION=$([regex]::Escape($workflowVersion))\s*$" -and
        $candidateText.Contains($expectedImport) -and $candidateText -notmatch 'FFVideoSource\s*\(') {
        $avs = $padfixAvs
    }
}

if ($Pass -eq 'validate') {
    $ffArgs = @('-hide_banner', '-v', 'error', '-f', 'avisynth', '-i', $avs,
        '-frames:v', '3', '-an', '-sn', '-f', 'null', '-')
    $probeLines = & $fp -v error -f avisynth -i $avs -select_streams v:0 `
        -show_entries stream=nb_frames,duration,r_frame_rate,width,height,sample_aspect_ratio -of json
    $probeRc = $LASTEXITCODE
    if ($probeRc -ne 0) { throw "ffprobe could not inspect AVS ($probeRc): $avs" }
    try { $stream = (@($probeLines) -join "`n" | ConvertFrom-Json).streams | Select-Object -First 1 }
    catch { throw "ffprobe returned invalid AVS metadata for $avs ($($_.Exception.Message))" }
    if (-not $stream -or [int64]$stream.nb_frames -le 0) { throw "AVS did not report a positive frame count: $avs" }
    $frames = [int64]$stream.nb_frames
    $duration = [double]$stream.duration
    if ($ep.video.frames -and [int64]$ep.video.frames -ne $frames) {
        throw "AVS frame count $frames disagrees with manifest video.frames=$($ep.video.frames)"
    }
    if ($ep.video.duration_s -and [math]::Abs($duration - [double]$ep.video.duration_s) -gt 0.002) {
        throw "AVS duration $duration disagrees with manifest video.duration_s=$($ep.video.duration_s)"
    }
} else {
    $dst = if ($Output) { [IO.Path]::GetFullPath($Output) } else { Resolve-LdFile $prof $ep 'padfixed_video' }
    Assert-LdWritablePath -Profile $prof -Path $dst
    if ([IO.Path]::GetExtension($dst) -ine '.mkv') { throw "final video output must be .mkv: $dst" }
    $frames = [int]$ep.video.frames
    if ($frames -le 0) { throw 'video.frames is not measured; run the validate pass and record the actual count.' }
    $duration = Get-LdNumber $ep.video.duration_s 'video.duration_s'
    $sarLines = & $fp -v error -select_streams v:0 -show_entries stream=sample_aspect_ratio `
        -of default=nk=1:nw=1 $stack
    $probeRc = $LASTEXITCODE
    $sar = @($sarLines | Select-Object -First 1)[0]
    if ($probeRc -ne 0 -or -not $sar) { throw "Could not read SAR from stack master: $stack (ffprobe exit $probeRc; SAR='$sar')" }
    # Apply the SAR as a DAR -aspect OUTPUT option, not `-vf setsar=<n>:<d>`: inside a
    # filtergraph ':' separates filter options, so `setsar=121:144` sets num=121 and
    # feeds 144 to setsar's unrelated 'max' option - FFmpeg 9.0.2 muxed SAR 121:1
    # (DAR 192:1) instead of 121:144. -aspect takes a plain rational (no filtergraph
    # parsing) and the container derives SAR exactly. DAR = SAR x width/height.
    if ($sar -notmatch '^(\d+):(\d+)$') { throw "Unexpected SAR '$sar' from stack master" }
    $sarNum = [int]$Matches[1]; $sarDen = [int]$Matches[2]
    $dimLine = (& $fp -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0 $stack | Select-Object -First 1)
    $wh = @($dimLine -split ',')
    if ($wh.Count -ne 2) { throw "Could not read stack dimensions from $stack (got '$dimLine')" }
    $aspect = "$($sarNum * [int]$wh[0]):$($sarDen * [int]$wh[1])"
    $tmp = $dst -replace '\.mkv$', '.tmp.mkv'
    $ffArgs = @('-y', '-hide_banner', '-stats', '-stats_period', '30',
        '-f', 'avisynth', '-i', $avs,
        '-map', '0:v:0', '-an', '-sn', '-aspect', $aspect,
        '-c:v', 'libx265', '-profile:v', 'main10', '-pix_fmt', 'yuv420p10le',
        '-crf', '14', '-preset', 'slow', '-tune', 'grain',
        '-x265-params', 'colorprim=smpte170m:transfer=smpte170m:colormatrix=smpte170m:range=limited:log-level=error:info=0',
        '-color_primaries', 'smpte170m', '-color_trc', 'smpte170m', '-colorspace', 'smpte170m',
        '-color_range', 'tv',
        '-frames:v', "$frames", '-t', $duration,
        '-metadata:s:v:0', "title=LaserDisc $($ep.audio.capture), measured IVTC with source PAD repairs, 23.976p",
        '-max_muxing_queue_size', '8192', $tmp)
}

$logRoot = Get-LdRoot $prof 'logs'
$log = Join-Path $logRoot "encode-$Episode-$Pass.log"
Write-Host "episode : $Episode ($($ep.title))"
Write-Host "pass    : $Pass"
Write-Host "AVS     : $avs"
if ($Pass -eq 'validate') {
    Write-Host "AVS metadata: $frames frames  duration=$duration  rate=$($stream.r_frame_rate)  raster=$($stream.width)x$($stream.height)"
    if ($DryRun) { Write-Host "DRY RUN - ffprobe plus 3-frame null decode: ffmpeg $($ffArgs -join ' ')"; exit 0 }
    $logRoot = Get-LdRoot $prof 'logs'
    New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
    $log = Join-Path $logRoot "encode-$Episode-validate.log"
    $rc = Invoke-LdFfmpeg -Ffmpeg $ff -Arguments $ffArgs -Log $log
    if ($rc -ne 0) { throw "AVS sample decode failed with exit $rc; inspect $log" }
    Write-Host "VALIDATE PASS - AVS metadata and first three decoded frames; no video encode was performed."
    exit 0
}
Write-Host "output  : $dst"
Write-Host "frames  : $frames  duration=$duration  SAR=$sar"
if ($DryRun) { Write-Host "DRY RUN - ffmpeg $($ffArgs -join ' ')"; exit 0 }

if (Test-Path -LiteralPath $dst) { throw "Refusing to overwrite existing output: $dst" }
if (Test-Path -LiteralPath $tmp) { throw "Temporary output exists; inspect it before retrying: $tmp" }
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dst), $logRoot | Out-Null
$rc = Invoke-LdFfmpeg -Ffmpeg $ff -Arguments $ffArgs -Log $log
if ($rc -ne 0) { throw "Encode failed with exit $rc; inspect $log and retained temporary file $tmp" }
if (-not (Test-Path -LiteralPath $tmp)) { throw "ffmpeg reported success but output is missing: $tmp" }
Move-Item -LiteralPath $tmp -Destination $dst
Write-Host "created $dst ($([math]::Round((Get-Item -LiteralPath $dst).Length/1GB,3)) GiB)"
Write-Host "The source-derived PAD-repaired AVS was encoded once (video-only); next stream-copy mux the validated audio and run delivery QC."
