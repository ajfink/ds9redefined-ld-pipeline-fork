<#
.SYNOPSIS
  Stream-copy the pad-corrected video together with this episode's audio/subtitle tracks.

.DESCRIPTION
  Which tracks are muxed and where they come from is decided by the manifest:
    mux_tracks            ["ld"] or ["ld","51","srt"]
    ld_audio_source       key of the track file holding the LD stereo
  so one script serves every episode without an episode-specific branch.

  Nothing is re-encoded: every output stream must be packet-identical to its
  source, which is asserted after the mux. Optional -AudioSource/-Output paths
  support isolated, non-manifest A/B timing tests; they never overwrite outputs.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File remux_padfixed.ps1 -Episode S01E05
  powershell -File remux_padfixed.ps1 -Episode S01E07 -AudioSource <candidate.flac> -Output <new-test.mkv>
#>
param(
    [Parameter(Mandatory)][string]$Episode,
    [string]$Video,
    [string]$AudioSource,
    [string]$Output,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'pipeline.ps1')

$prof = Get-LdProfile
$ep = Get-LdEpisode -Code $Episode
$ff = Get-LdTool $prof 'ffmpeg'
$fp = Get-LdTool $prof 'ffprobe'
$video = if ($Video) { $Video } else { Resolve-LdFile $prof $ep 'padfixed_video' }
$out = if ($Output) { $Output } else { Resolve-LdFile $prof $ep 'padfixed' }
$dur = Get-LdNumber $ep.video.duration_s 'video.duration_s'
$tracks = @($ep.mux_tracks)
if (-not $tracks) { throw "manifest has no mux_tracks - record ['ld'] or ['ld','51','srt']" }
$ldSrcKey = $ep.ld_audio_source
if (-not $AudioSource -and -not $ldSrcKey) { throw "manifest has no ld_audio_source" }
$ldSrc = if ($AudioSource) { $AudioSource } else { Resolve-LdFile $prof $ep $ldSrcKey }
$want51 = $tracks -contains '51'
$wantSrt = $tracks -contains 'srt'
if (($want51 -ne $wantSrt) -and $ep.format -ne 'one_part') {
    throw "mux_tracks mixes audio and subtitle inclusion inconsistently: $($tracks -join ',')"
}

foreach ($f in @($video, $ldSrc)) { if (-not (Test-Path -LiteralPath $f)) { throw "missing input: $f" } }
if (-not $DryRun -and (Test-Path -LiteralPath $out)) {
    throw "refusing to overwrite existing output: $out"
}

# ld_audio_source doubles as the source of the DVD-derived tracks when present
$full = Resolve-LdFile $prof $ep 'pre_upscale' -Optional
if ($want51) {
    if ($ldSrc -ne $full) {
        throw "mux_tracks includes 51/srt, so ld_audio_source ($ldSrcKey) must be the file that already holds them ($full)"
    }
    if (-not (Test-Path -LiteralPath $full)) { throw "missing input: $full" }
    $maps = @('-map', '0:v:0', '-map', '1:a:0', '-map', '1:a:1', '-map', '1:s:0')
    $meta = @('-metadata:s:a:0', 'title=LD 2.0 (original Dolby Surround)', '-metadata:s:a:0', 'language=eng',
              '-metadata:s:a:1', 'title=DVD 5.1 (retimed)', '-metadata:s:a:1', 'language=eng',
              '-metadata:s:s:0', 'title=English SDH (retimed)', '-metadata:s:s:0', 'language=eng',
              '-disposition:a:0', 'default', '-disposition:a:1', '0', '-disposition:s:0', '0')
} else {
    $maps = @('-map', '0:v:0', '-map', '1:a:0')
    $meta = @('-metadata:s:a:0', 'title=LD 2.0 (original Dolby Surround)', '-metadata:s:a:0', 'language=eng',
              '-disposition:a:0', 'default')
}

$ffArgs = @('-y', '-hide_banner', '-loglevel', 'warning', '-i', $video, '-i', $ldSrc) + $maps +
    @('-c', 'copy', '-map_metadata', '1',
      '-metadata:s:v:0', "title=LaserDisc $($ep.audio.capture), 3:2 pulldown removed, 23.976p, PAD blips repaired",
      '-disposition:v:0', 'default') + $meta +
    @('-color_primaries', 'smpte170m', '-color_trc', 'smpte170m',
      '-colorspace', 'smpte170m', '-color_range', 'tv', '-t', $dur, $out)

Write-Host "episode : $Episode  ($($ep.title))"
Write-Host "tracks  : $($tracks -join ' + ')"
Write-Host "video   : $video"
Write-Host "ld src  : $ldSrc"
Write-Host "output  : $out  (-t $dur)"
if ($DryRun) {
    Write-Host ''
    Write-Host "DRY RUN - ffmpeg $($ffArgs -join ' ')"
    exit 0
}

$rc = Invoke-LdFfmpeg -Ffmpeg $ff -Arguments $ffArgs
if ($rc -ne 0) { throw "remux failed for $Episode (exit $rc)" }
Write-Host "created $out ($([math]::Round((Get-Item -LiteralPath $out).Length / 1GB, 3)) GiB)"

# ---- gate: stream inventory and dispositions ----
$probeJson = & $fp -v error -show_entries `
    'stream=index,codec_type,codec_name,profile,pix_fmt,width,height,sample_aspect_ratio,sample_rate,channels,channel_layout,bits_per_raw_sample:stream_disposition=default:stream_tags=language,title' `
    -of json $out
if ($LASTEXITCODE -ne 0) { throw "ffprobe failed on $out" }
$probe = $probeJson | ConvertFrom-Json
$va = @($probe.streams | Where-Object codec_type -eq 'video')
$aa = @($probe.streams | Where-Object codec_type -eq 'audio')
$ss = @($probe.streams | Where-Object codec_type -eq 'subtitle')
$wantA = if ($want51) { 2 } else { 1 }
$wantS = if ($wantSrt) { 1 } else { 0 }
if ($va.Count -ne 1 -or $aa.Count -ne $wantA -or $ss.Count -ne $wantS) {
    throw "stream counts wrong: video=$($va.Count) audio=$($aa.Count) subtitle=$($ss.Count) (wanted 1/$wantA/$wantS)"
}
if ($aa[0].disposition.default -ne 1) { throw "LD stereo is not the default track" }
if ($want51 -and $aa[1].disposition.default -ne 0) { throw "DVD 5.1 must be non-default" }
if ($wantSrt -and $ss[0].disposition.default -ne 0) { throw "subtitles must be non-default" }
if (-not $want51 -and $ss.Count -gt 0) { throw "unexpected subtitle stream" }
foreach ($s in ($aa + $ss)) { if ($s.tags.language -ne 'eng') { throw "stream missing language=eng" } }
Write-Host "gate PASS: $($va.Count) video, $($aa.Count) audio (LD default), $($ss.Count) subtitle"

# Stream-copy is a hard identity requirement. Compare packet MD5s against the
# exact sources that were mapped, not just stream inventory/dispositions.
function Get-PacketMd5([string]$Path, [string]$Selector) {
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $lines = & $ff -hide_banner -v error -i $Path -map $Selector -c copy -f hash -hash md5 - 2>&1
    $rc = $LASTEXITCODE
    $ErrorActionPreference = $oldEap
    if ($rc -ne 0) { throw "packet-hash failed for $Path $Selector (exit $rc): $($lines -join ' ')" }
    $hit = @($lines | ForEach-Object { $_.ToString() } | Where-Object { $_ -match '^MD5=[0-9a-fA-F]{32}$' })
    if ($hit.Count -ne 1) { throw "could not read a unique packet MD5 for $Path ${Selector}: $($lines -join ' ')" }
    return $hit[0].Substring(4).ToLowerInvariant()
}
$hashPairs = @(
    [pscustomobject]@{ Label='video'; Source=$video; SourceMap='0:v:0'; OutputMap='0:v:0' },
    [pscustomobject]@{ Label='LD audio'; Source=$ldSrc; SourceMap='0:a:0'; OutputMap='0:a:0' }
)
if ($want51) {
    $hashPairs += [pscustomobject]@{ Label='DVD 5.1'; Source=$full; SourceMap='0:a:1'; OutputMap='0:a:1' }
    $hashPairs += [pscustomobject]@{ Label='English SDH'; Source=$full; SourceMap='0:s:0'; OutputMap='0:s:0' }
}
foreach ($pair in $hashPairs) {
    $sourceHash = Get-PacketMd5 $pair.Source $pair.SourceMap
    $outputHash = Get-PacketMd5 $out $pair.OutputMap
    if ($sourceHash -ne $outputHash) {
        throw "$($pair.Label) packet hash changed: source=$sourceHash output=$outputHash"
    }
    Write-Host "  packet identity PASS: $($pair.Label) $outputHash"
}

foreach ($s in ($aa + $ss)) {
    Write-Host ("  [{0}] {1}  default={2}  {3}" -f $s.codec_name, $s.tags.title, $s.disposition.default, $s.tags.language)
}
Write-Host "next: run qc_report.py --episode $Episode"
