<#
.SYNOPSIS
  Preflight for the LaserDisc pipeline: discover/validate every tool, root and
  Python dependency the runbook needs, on THIS machine.

.DESCRIPTION
  Reads an explicit device-local machine profile (the desktop app creates one
  under its unique devices/<device>-<id>/local device config folder) or, for a
  standalone legacy bundle, machine.json beside this script. Discovery can fill
  a new profile with -Write. Nothing else in the pipeline should
  hard-code a drive letter, a user profile or a tool version.

  Exit codes: 0 = all required checks PASS (warnings allowed)
              1 = at least one required check FAILED
              2 = profile missing/unreadable

  Discovery is best-effort: a filled-in profile always wins over discovery, so
  a machine that does not follow the common layout simply has to be described.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File preflight.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File preflight.ps1 -Write
  powershell -NoProfile -ExecutionPolicy Bypass -File preflight.ps1 -Episode S01E05
#>
param(
    [string]$Profile = '',
    [switch]$Write,
    [string]$Episode = ''
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$explicitProfile = [bool]$Profile -or [bool]$env:LDP_PROFILE
if (-not $Profile -and $env:LDP_PROFILE) { $Profile = $env:LDP_PROFILE }
if (-not $Profile) { $Profile = Join-Path $here 'machine.json' }
$template = Join-Path $here 'machine.example.json'
$sharedDevices = Join-Path $here 'devices'

function Test-DeviceScopedProfile([string]$Candidate, [string]$ReleaseRoot) {
    try {
        if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
        $machineProfile = Get-Content -LiteralPath $Candidate -Raw | ConvertFrom-Json
        $shared = $machineProfile.shared
        if (-not $shared -or $shared.enabled -ne $true) { return $false }
        if ([string]::IsNullOrWhiteSpace([string]$machineProfile.machine) -or
            $machineProfile.machine -ine $env:COMPUTERNAME) { return $false }
        if (-not $shared.release_root -or
            [IO.Path]::GetFullPath([string]$shared.release_root) -ine [IO.Path]::GetFullPath($ReleaseRoot)) { return $false }
        $device = [string]$shared.device_name
        if ([string]::IsNullOrWhiteSpace($device)) { $device = [string]$machineProfile.machine }
        $device = (($device -replace '[^A-Za-z0-9._-]+', '-') -replace '^[._-]+|[._-]+$', '')
        $id = ([string]$shared.device_instance_id).ToUpperInvariant()
        if (-not $device -or $id -notmatch '^[0-9A-F]{8}$') { return $false }
        $expected = Join-Path $ReleaseRoot "devices\$device-$id\local device config\machine.json"
        if ([IO.Path]::GetFullPath($Candidate) -ine [IO.Path]::GetFullPath($expected)) { return $false }

        $identityRoot = if ($env:LDP_DEVICE_ID_DIR) { $env:LDP_DEVICE_ID_DIR } else { Join-Path $HOME '.config\ld-pipeline\device-identities' }
        $identityPath = Join-Path $identityRoot "$device.json"
        if (-not (Test-Path -LiteralPath $identityPath -PathType Leaf)) { return $false }
        $identity = Get-Content -LiteralPath $identityPath -Raw | ConvertFrom-Json
        return ($identity.schema -eq 'ld-pipeline-device-identity-v1' -and
                $identity.device_name -eq $device -and
                $identity.machine -eq $machineProfile.machine -and
                ([string]$identity.instance_id).ToUpperInvariant() -eq $id)
    } catch {
        return $false
    }
}

if ((Test-Path -LiteralPath $sharedDevices -PathType Container) -and -not $explicitProfile) {
    Write-Host "Shared release detected at $here - select this device's config under devices/<device>-<id>/local device config."
    exit 2
}
if ((Test-Path -LiteralPath $sharedDevices -PathType Container) -and
    [System.IO.Path]::GetFullPath($Profile).StartsWith(([System.IO.Path]::GetFullPath($here).TrimEnd('\') + '\'), [StringComparison]::OrdinalIgnoreCase) -and
    -not (Test-DeviceScopedProfile $Profile $here)) {
    Write-Host "Profile inside the shared release must be this machine's verified device config: $Profile"
    exit 2
}

$script:results = New-Object System.Collections.Generic.List[object]
function Add-Result([string]$name, [string]$status, [string]$detail) {
    $script:results.Add([pscustomobject]@{ Check = $name; Status = $status; Detail = $detail })
}
function Find-Exe([string]$leaf, [string[]]$roots, [int]$depth = 4) {
    $cmd = Get-Command $leaf -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($r in $roots) {
        if (-not $r -or -not (Test-Path -LiteralPath $r)) { continue }
        $hit = Get-ChildItem -LiteralPath $r -Filter $leaf -Recurse -Depth $depth -ErrorAction SilentlyContinue |
               Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

# ---- profile ---------------------------------------------------------------
if (-not (Test-Path -LiteralPath $template)) { Write-Host "MISSING template: $template"; exit 2 }
$haveProfile = Test-Path -LiteralPath $Profile
if (-not $haveProfile -and -not $Write) {
    Write-Host "No profile at $Profile - run with -Write to create one (discovery), or copy machine.example.json."
    exit 2
}
if ($Write -and $haveProfile) {
    $stamp = Get-Date -Format 'yyyyMMddTHHmmssfff'
    $profileBackup = "$Profile.backup-$stamp"
    try {
        Copy-Item -LiteralPath $Profile -Destination $profileBackup -ErrorAction Stop
        Add-Result 'profile-backup' 'PASS' $profileBackup
    } catch {
        Add-Result 'profile-backup' 'FAIL' "could not preserve existing profile: $($_.Exception.Message)"
        Write-Host "Refusing -Write because the current profile could not be backed up: $Profile"
        exit 2
    }
}
$src = if ($haveProfile) { $Profile } else { $template }
$prof = Get-Content -LiteralPath $src -Raw | ConvertFrom-Json
$resetCopiedProfile = $false
if ($Write -and -not $explicitProfile -and $haveProfile -and $prof.machine -and
    $prof.machine -ne 'UNNAMED' -and $prof.machine -ne $env:COMPUTERNAME) {
    # The shipped machine.json describes the authoring PC. When the whole folder
    # is copied to another host, do not let those stale absolute paths masquerade
    # as a new machine profile; start from the portable template instead.
    $prof = Get-Content -LiteralPath $template -Raw | ConvertFrom-Json
    $resetCopiedProfile = $true
}
if (-not $prof.machine -or $prof.machine -eq 'UNNAMED') { $prof.machine = $env:COMPUTERNAME }
if ((Test-Path -LiteralPath $sharedDevices -PathType Container) -and
    (-not $prof.shared -or -not $prof.shared.enabled -or
     [System.IO.Path]::GetFullPath([string]$prof.shared.release_root) -ne [System.IO.Path]::GetFullPath($here))) {
    Write-Host "Shared release profile must set shared.enabled=true and shared.release_root=$here."
    exit 2
}
if ($resetCopiedProfile) { Add-Result 'profile' 'WARN' "copied profile belonged to another machine; initialized from template for $env:COMPUTERNAME" }
elseif (-not $haveProfile) { Add-Result 'profile' 'WARN' "created from template by discovery" }
else { Add-Result 'profile' 'PASS' $Profile }

# ---- tool discovery --------------------------------------------------------
$ffRoots = @((Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'), 'C:\ffmpeg', (Join-Path $here 'tools'))
if (-not $prof.tools.ffmpeg -or -not (Test-Path -LiteralPath $prof.tools.ffmpeg)) {
    $prof.tools.ffmpeg = Find-Exe 'ffmpeg.exe' $ffRoots
}
if (-not $prof.tools.ffprobe -or -not (Test-Path -LiteralPath $prof.tools.ffprobe)) {
    $ffdir = if ($prof.tools.ffmpeg) { Split-Path -Parent $prof.tools.ffmpeg } else { '' }
    $p = if ($ffdir) { Join-Path $ffdir 'ffprobe.exe' } else { $null }
    if (-not $p -or -not (Test-Path -LiteralPath $p)) { $p = Find-Exe 'ffprobe.exe' $ffRoots }
    $prof.tools.ffprobe = $p
}
if (-not $prof.tools.orc -or -not (Test-Path -LiteralPath $prof.tools.orc)) {
    $orcRoots = @("$env:ProgramFiles", "${env:ProgramFiles(x86)}", (Join-Path $here 'tools'))
    $prof.tools.orc = Find-Exe 'orc-cli.exe' $orcRoots
}
if (-not $prof.tools.rife_engine -or -not (Test-Path -LiteralPath $prof.tools.rife_engine)) {
    $rifeRoots = @((Join-Path $env:LOCALAPPDATA 'Flowframes\FlowframesData\pkgs'), (Join-Path $here 'tools'))
    $prof.tools.rife_engine = Find-Exe 'rife-ncnn-vulkan.exe' $rifeRoots 5
}
if (-not $prof.tools.rife_model_dir -or -not (Test-Path -LiteralPath $prof.tools.rife_model_dir)) {
    $roots = @((Join-Path $env:LOCALAPPDATA 'Flowframes\FlowframesData\pkgs'), (Join-Path $here 'models'))
    foreach ($r in $roots) {
        if (-not $r -or -not (Test-Path -LiteralPath $r)) { continue }
        $hit = Get-ChildItem -LiteralPath $r -Directory -Recurse -Depth 3 -ErrorAction SilentlyContinue |
               Where-Object { $_.Name -match '^rife-v[0-9]' } | Select-Object -First 1
        if ($hit) { $prof.tools.rife_model_dir = $hit.FullName; break }
    }
}
if (-not $prof.tools.avisynth_plugin_dir -or -not (Test-Path -LiteralPath $prof.tools.avisynth_plugin_dir)) {
    $avsRoots = @((Join-Path $here 'avsplugins'),
                  (Join-Path $env:APPDATA 'AviSynth\plugins'), "$env:ProgramFiles\AviSynth+\plugins")
    foreach ($r in $avsRoots) { if ($r -and (Test-Path -LiteralPath $r)) { $prof.tools.avisynth_plugin_dir = $r; break } }
}
if (-not $prof.tools.curl -or -not (Test-Path -LiteralPath $prof.tools.curl)) {
    $curlCmd = Get-Command 'curl.exe' -ErrorAction SilentlyContinue
    if ($curlCmd) { $prof.tools.curl = $curlCmd.Source }
}
if (-not $prof.tools.sevenzip -or -not (Test-Path -LiteralPath $prof.tools.sevenzip)) {
    $prof.tools.sevenzip = Find-Exe '7z.exe' @("$env:ProgramFiles\7-Zip", 'C:\Program Files\7-Zip')
}

# ---- root discovery (best effort; a filled-in profile always wins) ---------
# Prefer an explicit environment hint, then common user locations, then a
# conventional <drive>\work folder with a probe child. Custom layouts belong
# in machine.json rather than being added as another drive-letter exception.
$fileRoots = @(Get-PSDrive -PSProvider FileSystem | ForEach-Object { $_.Root } | Where-Object { $_ })
$workCandidates = @($env:LDP_WORK_ROOT,
    $(if ($env:USERPROFILE) { Join-Path $env:USERPROFILE 'work' }),
    'C:\work') + @($fileRoots | ForEach-Object { Join-Path $_ 'work' })
if (-not $prof.roots.work -or -not (Test-Path -LiteralPath $prof.roots.work)) {
    $prof.roots.work = ''
    $envWork = $env:LDP_WORK_ROOT
    if ($envWork -and (Test-Path -LiteralPath $envWork -PathType Container)) {
        $prof.roots.work = [IO.Path]::GetFullPath($envWork)
        Add-Result 'root-discovery:work' 'PASS' "explicit LDP_WORK_ROOT=$($prof.roots.work)"
    } else {
        $existingWork = @(
            foreach ($c in $workCandidates) {
                if ($c -and (Test-Path -LiteralPath $c -PathType Container)) {
                    try { [IO.Path]::GetFullPath($c).TrimEnd('\') } catch { }
                }
            }
        ) | Sort-Object -Unique
        $withProbe = @($existingWork | Where-Object {
            Test-Path -LiteralPath (Join-Path $_ 'probe') -PathType Container
        })
        if ($withProbe.Count -eq 1) {
            $prof.roots.work = $withProbe[0]
            Add-Result 'root-discovery:work' 'PASS' "unique work candidate with probe: $($prof.roots.work)"
        } elseif ($withProbe.Count -gt 1) {
            Add-Result 'root-discovery:work' 'WARN' "ambiguous work roots with probe; set roots.work/LDP_WORK_ROOT explicitly: $($withProbe -join ', ')"
        } elseif ($existingWork.Count -eq 1) {
            $prof.roots.work = $existingWork[0]
            Add-Result 'root-discovery:work' 'PASS' "unique existing work candidate: $($prof.roots.work)"
        } elseif ($existingWork.Count -gt 1) {
            Add-Result 'root-discovery:work' 'WARN' "ambiguous work roots; set roots.work/LDP_WORK_ROOT explicitly: $($existingWork -join ', ')"
        }
    }
}
if ($prof.roots.work) {
    foreach ($r in 'probe', 'avs', 'audio', 'subs', 'out', 'prores', 'stack', 'logs') {
        if (-not $prof.roots.$r) { $prof.roots.$r = Join-Path $prof.roots.work $r }
    }
}
if (-not $prof.roots.downloads_root) {
    $downloadRoots = @(
        foreach ($d in $fileRoots) {
        if (-not (Test-Path -LiteralPath $d)) { continue }
        $hit = Get-ChildItem -LiteralPath $d -Directory -Filter 'DS9 S01E*' -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($hit) { [IO.Path]::GetFullPath($d) }
        }
    ) | Sort-Object -Unique
    if ($downloadRoots.Count -eq 1) {
        $prof.roots.downloads_root = $downloadRoots[0]
    } elseif ($downloadRoots.Count -gt 1) {
        Add-Result 'root-discovery:downloads' 'WARN' "ambiguous download roots; set roots.downloads_root explicitly: $($downloadRoots -join ', ')"
    }
}
if (-not $prof.roots.runbook_dir) {
    foreach ($d in @($env:LDP_RUNBOOK_DIR, $here) + $fileRoots) {
        if (-not $d -or -not (Test-Path -LiteralPath $d)) { continue }
        if (Test-Path -LiteralPath (Join-Path $d 'DS9 LaserDisc OpenCode Runbook.md')) { $prof.roots.runbook_dir = $d; break }
    }
}

# ---- required tool checks --------------------------------------------------
foreach ($t in 'ffmpeg', 'ffprobe', 'orc', 'rife_engine', 'rife_model_dir', 'avisynth_plugin_dir', 'curl') {
    $v = $prof.tools.$t
    if ($v -and (Test-Path -LiteralPath $v)) {
        $d = $v
        if ((Get-Item -LiteralPath $v) -is [System.IO.FileInfo]) { $d = "$v ($([math]::Round((Get-Item $v).Length/1MB,1)) MB)" }
        Add-Result $t 'PASS' $d
    } else {
        Add-Result $t 'FAIL' 'not found - set it in machine.json or install the tool'
    }
}
Add-Result 'sevenzip' $(if ($prof.tools.sevenzip -and (Test-Path -LiteralPath $prof.tools.sevenzip)) { 'PASS' } else { 'WARN' }) `
    $(if ($prof.tools.sevenzip -and (Test-Path -LiteralPath $prof.tools.sevenzip)) { $prof.tools.sevenzip } else { 'absent - only needed for .efm.7z inputs' })
# ---- required runtime capabilities (existence alone is not enough) ----------
if ($prof.tools.rife_model_dir -and (Test-Path -LiteralPath $prof.tools.rife_model_dir)) {
    $bins = @(Get-ChildItem -LiteralPath $prof.tools.rife_model_dir -Filter '*.bin' -File -Recurse -ErrorAction SilentlyContinue)
    $params = @(Get-ChildItem -LiteralPath $prof.tools.rife_model_dir -Filter '*.param' -File -Recurse -ErrorAction SilentlyContinue)
    if ($bins.Count -gt 0 -and $params.Count -gt 0) {
        Add-Result 'rife-model-files' 'PASS' "$($bins.Count) .bin and $($params.Count) .param files"
    } else {
        Add-Result 'rife-model-files' 'FAIL' 'model directory has no matching .bin/.param weights'
    }
}
if ($prof.tools.avisynth_plugin_dir -and (Test-Path -LiteralPath $prof.tools.avisynth_plugin_dir)) {
    foreach ($leaf in 'ffms2.dll', 'RemapFrames_x64.dll') {
        if (Test-Path -LiteralPath (Join-Path $prof.tools.avisynth_plugin_dir $leaf)) {
            Add-Result "avs-plugin:$leaf" 'PASS' (Join-Path $prof.tools.avisynth_plugin_dir $leaf)
        } else {
            Add-Result "avs-plugin:$leaf" 'FAIL' "missing $leaf from the configured AviSynth plugin directory"
        }
    }
}
if ($prof.tools.ffmpeg -and (Test-Path -LiteralPath $prof.tools.ffmpeg)) {
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $demux = (& $prof.tools.ffmpeg -hide_banner -demuxers 2>&1 | Out-String)
    $encoders = (& $prof.tools.ffmpeg -hide_banner -encoders 2>&1 | Out-String)
    $ErrorActionPreference = $oldEap
    if ($demux -match '(?m)^\s*D\s+avisynth\s') { Add-Result 'ffmpeg:avisynth' 'PASS' 'AviSynth input demuxer available' }
    else { Add-Result 'ffmpeg:avisynth' 'FAIL' 'ffmpeg lacks the AviSynth demuxer required by padfix AVS' }
    if ($encoders -match '(?m)^\s*V\S*\s+libx265\s') { Add-Result 'ffmpeg:libx265' 'PASS' 'libx265 encoder available' }
    else { Add-Result 'ffmpeg:libx265' 'FAIL' 'ffmpeg lacks libx265 required by the CRF 14/Main 10 encode' }

    if ($demux -match '(?m)^\s*D\s+avisynth\s') {
        $smokeAvs = Join-Path ([IO.Path]::GetTempPath()) ("ld-preflight-{0}.avs" -f [guid]::NewGuid().ToString('N'))
        $avsLoaderPath = $prof.tools.avisynth_loader
        if ($avsLoaderPath) {
            if (Test-Path -LiteralPath $avsLoaderPath -PathType Leaf) {
                Add-Result 'avs:loader' 'PASS' $avsLoaderPath
            } else {
                Add-Result 'avs:loader' 'FAIL' "configured loader missing: $avsLoaderPath"
                $avsLoaderPath = $null
            }
        }
        $smokeText = @'
Assert(FunctionExists("LSMASHVideoSource"), "LSMASHVideoSource missing")
Assert(FunctionExists("TFM"), "TFM missing")
Assert(FunctionExists("TDecimate"), "TDecimate missing")
src = BlankClip(width=64, height=48, length=15, fps=30000, pixel_type="YV12").AssumeTFF()
fm = src.TFM(order=-1, pp=0, chroma=true, slow=2)
out = fm.TDecimate(mode=1, cycle=5, cycleR=1)
return out
'@
        if ($avsLoaderPath) {
            $loaderAvs = ([string]$avsLoaderPath).Replace('\', '\\').Replace('"', '\"')
            $smokeText = "Import(`"$loaderAvs`")`r`n$smokeText"
        }
        try {
            Set-Content -LiteralPath $smokeAvs -Value $smokeText -Encoding ASCII
            $oldEap = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            $smokeOutput = & $prof.tools.ffmpeg -hide_banner -v error -nostdin `
                -f avisynth -i $smokeAvs -map 0:v:0 -an -frames:v 3 -f framemd5 - 2>&1
            $smokeRc = $LASTEXITCODE
            $ErrorActionPreference = $oldEap
            $frameRows = @($smokeOutput | Where-Object { [string]$_ -match '^\s*0,\s*\d+' })
            if ($smokeRc -eq 0 -and $frameRows.Count -eq 3) {
                Add-Result 'avs:tfm-tdecimate-smoke' 'PASS' 'LSMASHSource/TFM/TDecimate loaded; 3 output frames decoded'
            } else {
                $detail = ($smokeOutput | Out-String).Trim()
                Add-Result 'avs:tfm-tdecimate-smoke' 'FAIL' "exit=$smokeRc, frames=$($frameRows.Count); verify LSMASH/TIVTC plugins or configure tools.avisynth_loader; $detail"
            }
        } catch {
            Add-Result 'avs:tfm-tdecimate-smoke' 'FAIL' $_.Exception.Message
        } finally {
            $ErrorActionPreference = $oldEap
            if (Test-Path -LiteralPath $smokeAvs) {
                Remove-Item -LiteralPath $smokeAvs -Force -ErrorAction SilentlyContinue
            }
        }
    } else {
        Add-Result 'avs:tfm-tdecimate-smoke' 'SKIP' 'requires FFmpeg AviSynth demuxer'
    }
}
Add-Result 'avs:qtgmc' 'WARN' 'conditional: verify QTGMC and dependencies only if cadence analysis finds interlaced/hybrid inserts'

foreach ($leaf in 'download_archive.ps1', 'init_episode.py', 'init_local_episode.py', 'local_source.py',
    'workflow_app.py', 'workflow_app_model.py', 'review_popup.py', 'opencode_help.py',
    'pad_review.py', 'render_stack.py', 'retime_break_review.py', 'validate_episode.py',
    'extract_video_descriptors.py', 'measure_dvd_alignment.py', 'measure_break_edges.py',
    'prepare_retime_breaks.py', 'record_retime_breaks.py', 'retime_break_candidates.py',
    'measure_audio_rate.py',
    'build_map.ps1', 'encode_episode.ps1', 'build_stack_project.py', 'retime_dvd_assets.py',
    'check_space_budget.py', 'stage_clock.py', 'run_workflow.py',
    'measure_pad_audio.py', 'measure_ld_black.py', 'efm_paths.py') {
    $scriptPath = Join-Path $here "scripts\$leaf"
    if (Test-Path -LiteralPath $scriptPath) { Add-Result "bundle-script:$leaf" 'PASS' $scriptPath }
    else { Add-Result "bundle-script:$leaf" 'FAIL' "required workflow script missing: $scriptPath" }
}
if (-not (Test-Path -LiteralPath (Join-Path $here 'templates\stack_template.orcprj'))) {
    Add-Result 'bundle-template:stack' 'FAIL' 'templates\stack_template.orcprj is missing'
} else {
    Add-Result 'bundle-template:stack' 'PASS' (Join-Path $here 'templates\stack_template.orcprj')
}
$workflowVersionPath = Join-Path $here 'WORKFLOW_VERSION'
if (Test-Path -LiteralPath $workflowVersionPath) {
    $workflowVersion = (Get-Content -LiteralPath $workflowVersionPath -Raw).Trim()
    if ($workflowVersion -match '^[1-9]\d*$') {
        Add-Result 'workflow:version' 'PASS' "Version $workflowVersion"
    } else {
        Add-Result 'workflow:version' 'FAIL' "invalid WORKFLOW_VERSION value: '$workflowVersion'"
    }
} else {
    Add-Result 'workflow:version' 'FAIL' 'WORKFLOW_VERSION is missing'
}
foreach ($relativePath in 'WORKFLOW_VERSIONING.md', 'templates\device-workflow-change.template.md') {
    $changePath = Join-Path $here $relativePath
    if (Test-Path -LiteralPath $changePath) {
        Add-Result "workflow-versioning:$relativePath" 'PASS' $changePath
    } else {
        Add-Result "workflow-versioning:$relativePath" 'FAIL' "missing: $changePath"
    }
}

# ---- Python + packages -----------------------------------------------------
$py = $prof.python.interpreter
if (-not $py) { $py = 'python' }
$pyCmd = Get-Command $py -ErrorAction SilentlyContinue
if (-not $pyCmd) {
    Add-Result 'python' 'FAIL' "interpreter '$py' not on PATH"
} else {
    $ver = & $py -c "import sys;print('%d.%d.%d'%sys.version_info[:3])" 2>$null
    Add-Result 'python' 'PASS' "$($pyCmd.Source) ($ver)"
    foreach ($pkg in $prof.python.require) {
        $oldPackageEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $packageOutput = & $py -c "import importlib,sys; m=importlib.import_module(sys.argv[1]); print(getattr(m,'__version__','?'))" $pkg 2>&1
            $packageRc = $LASTEXITCODE
        } catch {
            $packageOutput = @($_.Exception.Message)
            $packageRc = 127
        } finally {
            $ErrorActionPreference = $oldPackageEap
        }
        if ($packageRc -eq 0) {
            $packageVersion = ($packageOutput | Out-String).Trim()
            Add-Result "python:$pkg" 'PASS' "import ok ($packageVersion)"
        } else {
            $hint = switch ($pkg) { 'cv2' { 'pip install opencv-python  - REQUIRED by the replacement-identity gate' }
                                    'PIL' { 'pip install pillow         - REQUIRED by contact sheets/thumbnails' }
                                    'numpy' { 'pip install numpy          - REQUIRED by nearly every script' }
                                    default { "pip install $pkg" } }
            $failure = ($packageOutput | Out-String).Trim()
            if ($failure.Length -gt 300) { $failure = $failure.Substring($failure.Length - 300) }
            Add-Result "python:$pkg" 'FAIL' "$hint; import failed (exit $packageRc): $failure"
        }
    }
}

# ---- roots -----------------------------------------------------------------
foreach ($r in 'work', 'probe', 'avs', 'audio', 'subs', 'out', 'prores', 'stack', 'logs', 'dvd_root') {
    $v = $prof.roots.$r
    if ($v -and (Test-Path -LiteralPath $v)) { Add-Result "root:$r" 'PASS' $v }
    else { Add-Result "root:$r" 'FAIL' 'missing - set it in machine.json' }
}
foreach ($r in 'downloads_root', 'runbook_dir', 'upscale_root') {
    $v = $prof.roots.$r
    if ($v -and (Test-Path -LiteralPath $v)) { Add-Result "root:$r" 'PASS' $v }
    elseif ($r -eq 'downloads_root') { Add-Result "root:$r" 'WARN' 'not set - needed for download scripts' }
    elseif ($r -eq 'upscale_root') { Add-Result "root:$r" 'WARN' 'not set - needed by legacy/outside-worktree deliverables such as E01' }
    else { Add-Result "root:$r" 'WARN' 'not set' }
}

# ---- free space ------------------------------------------------------------
$minGb = 80
if ($prof.limits -and $prof.limits.min_free_gb) { $minGb = [double]$prof.limits.min_free_gb }
if ($prof.roots.work -and (Test-Path -LiteralPath $prof.roots.work)) {
    $drv = (Get-Item -LiteralPath $prof.roots.work).PSDrive
    $freeGb = [math]::Round($drv.Free / 1GB, 1)
    if ($freeGb -ge $minGb) { Add-Result 'free-space' 'PASS' "$freeGb GiB on $($drv.Name): (min $minGb)" }
    else { Add-Result 'free-space' 'FAIL' "$freeGb GiB on $($drv.Name): - below required $minGb GiB" }
}

# ---- optional: one episode manifest ---------------------------------------
if ($Episode) {
    if ($prof.shared -and $prof.shared.enabled) {
        if (-not $env:LDP_MANIFEST_DIR) {
            Add-Result "episode:$Episode" 'FAIL' 'no active episode workspace; run shared_workspace.py ensure or use run_stage.py to resolve/create one with local fallback'
            $mp = $null
        } else { $mp = Join-Path $env:LDP_MANIFEST_DIR "$Episode.json" }
    } else { $mp = Join-Path $here "episodes\$Episode.json" }
    $ep = $null
    if ($mp -and (Test-Path -LiteralPath $mp)) {
        Add-Result "episode:$Episode" 'PASS' $mp
        try { $ep = Get-Content -LiteralPath $mp -Raw | ConvertFrom-Json }
        catch { Add-Result "episode:$Episode" 'FAIL' "manifest is not valid JSON: $($_.Exception.Message)" }
    } else {
        if ($mp) { Add-Result "episode:$Episode" 'FAIL' "manifest missing: $mp" }
    }
    $dl = $null
    if ($ep -and $ep.roots -and $ep.roots.downloads) {
        $dl = $ep.roots.downloads
    } elseif ($prof.roots.downloads_root -and $prof.roots.downloads_template) {
        $leaf = $prof.roots.downloads_template -replace '\{episode\}', $Episode
        $dl = Join-Path $prof.roots.downloads_root $leaf
    }
    if ($dl) {
        if (Test-Path -LiteralPath $dl) { Add-Result 'episode-inputs' 'PASS' $dl }
        else { Add-Result 'episode-inputs' 'WARN' "download folder not staged yet: $dl" }
    } else {
        Add-Result 'episode-inputs' 'WARN' 'no downloads_root/template in machine.json'
    }
    if ($ep -and $ep.files -and $ep.files.dvd) {
        $spec = $ep.files.dvd
        $dvdPath = $null
        if ($spec.path) { $dvdPath = $spec.path }
        elseif ($spec.root -and $spec.name) {
            $base = $prof.roots.($spec.root)
            if (-not $base) { Add-Result 'episode-dvd' 'FAIL' "files.dvd uses root '$($spec.root)' but it is not set in machine.json" }
            else { $dvdPath = Join-Path $base $spec.name }
        }
        if ($dvdPath) {
            if (Test-Path -LiteralPath $dvdPath) { Add-Result 'episode-dvd' 'PASS' $dvdPath }
            else { Add-Result 'episode-dvd' 'WARN' "DVD reference not staged at $dvdPath" }
        } elseif (-not ($spec.root -and -not $prof.roots.($spec.root))) {
            Add-Result 'episode-dvd' 'FAIL' 'files.dvd must declare {root,name} or {path}'
        }
    }
}

# ---- report ----------------------------------------------------------------
$w = ($script:results | ForEach-Object { $_.Check.Length } | Measure-Object -Maximum).Maximum
if (-not $w) { $w = 20 }
Write-Host ''
Write-Host "PREFLIGHT  machine=$($prof.machine)  profile=$(if($haveProfile){$Profile}else{'<discovered>'})"
Write-Host ('-' * ($w + 66))
foreach ($r in $script:results) {
    $pad = $r.Check.PadRight($w)
    Write-Host ("  [{0}] {1}  {2}" -f $r.Status, $pad, $r.Detail)
}
$fail = @($script:results | Where-Object Status -eq 'FAIL')
$warn = @($script:results | Where-Object Status -eq 'WARN')
Write-Host ('-' * ($w + 66))
Write-Host ("  {0} pass, {1} warn, {2} fail" -f (@($script:results | Where-Object Status -eq 'PASS')).Count, $warn.Count, $fail.Count)

if ($Write) {
    $out = ConvertTo-Json $prof -Depth 8
    [System.IO.File]::WriteAllText($Profile, $out, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "  wrote $Profile"
}
Write-Host ''
if ($fail.Count -gt 0) { Write-Host "PREFLIGHT FAILED - fix the items above before starting an episode."; exit 1 }
Write-Host 'PREFLIGHT OK.'
exit 0
