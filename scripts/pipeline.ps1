<#
  Shared helpers for the LaserDisc pipeline PowerShell scripts.

  Dot-source from a sibling script:
      . (Join-Path $PSScriptRoot 'pipeline.ps1')
  Nothing here hard-codes a drive letter or an episode constant.
#>

$script:LdBundle = Split-Path -Parent $PSScriptRoot

function Get-LdProfile {
    <# Returns the machine profile object, or fails with an actionable message. #>
    $p = $env:LDP_PROFILE
    $sharedDevices = Join-Path $script:LdBundle 'devices'
    if (-not $p -and (Test-Path -LiteralPath $sharedDevices -PathType Container)) {
        throw "shared release detected at $script:LdBundle - set LDP_PROFILE to a local profile outside the release tree"
    }
    if (-not $p) { $p = Join-Path $script:LdBundle 'machine.json' }
    if (-not (Test-Path -LiteralPath $p)) {
        $alt = Join-Path $script:LdBundle 'machine.example.json'
        if (Test-Path -LiteralPath $alt) { $p = $alt }
        else { throw "no machine profile at $p - run preflight.ps1 -Write" }
    }
    try { $profile = Get-Content -LiteralPath $p -Raw | ConvertFrom-Json }
    catch { throw "profile is not valid JSON: $p ($($_.Exception.Message))" }
    $script:LdProfilePath = [System.IO.Path]::GetFullPath($p)
    if (Test-Path -LiteralPath $sharedDevices -PathType Container) {
        $bundleFull = [System.IO.Path]::GetFullPath($script:LdBundle).TrimEnd('\') + '\'
        $profileFull = [System.IO.Path]::GetFullPath($p)
        if ($profileFull.StartsWith($bundleFull, [StringComparison]::OrdinalIgnoreCase)) {
            throw "shared release profile must be outside ${script:LdBundle}: $p"
        }
        if (-not $profile.shared -or -not $profile.shared.enabled -or
            [System.IO.Path]::GetFullPath([string]$profile.shared.release_root) -ne [System.IO.Path]::GetFullPath($script:LdBundle)) {
            throw "local shared-mode profile must set shared.enabled=true and shared.release_root=$script:LdBundle"
        }
    }
    return $profile
}

function Get-LdTool {
    param([Parameter(Mandatory)]$Profile, [Parameter(Mandatory)][string]$Name)
    $v = $Profile.tools.$Name
    if (-not $v) { throw "tool '$Name' is not set in the profile - run preflight.ps1 -Write" }
    if (-not (Test-Path -LiteralPath $v)) { throw "tool '$Name' points at a missing path: $v" }
    return $v
}

function Get-LdRoot {
    param([Parameter(Mandatory)]$Profile, [Parameter(Mandatory)][string]$Name)
    if ($Name -eq 'logs' -and $Profile.shared -and $Profile.shared.enabled) {
        if (-not $env:LDP_INSTANCE_ROOT) { throw "no active episode workspace; run scripts/shared_workspace.py ensure or invoke through run_stage.py" }
        $instance = [System.IO.Path]::GetFullPath($env:LDP_INSTANCE_ROOT)
        if ($env:LDP_INSTANCE_MODE -eq 'local-fallback') {
            $bundle = [System.IO.Path]::GetFullPath($script:LdBundle).TrimEnd('\') + '\'
            if ($instance.StartsWith($bundle, [StringComparison]::OrdinalIgnoreCase)) {
                throw "device-local fallback must be outside the README-folder release: $instance"
            }
            return Join-Path $instance 'logs'
        }
        $devices = [System.IO.Path]::GetFullPath((Join-Path $script:LdBundle 'devices')).TrimEnd('\') + '\'
        if (-not $instance.StartsWith($devices, [StringComparison]::OrdinalIgnoreCase)) {
            throw "LDP_INSTANCE_ROOT must be beneath $devices"
        }
        return Join-Path $instance 'logs'
    }
    $v = $Profile.roots.$Name
    if (-not $v) { throw "root '$Name' is not set in the profile" }
    return $v
}

function Assert-LdWritablePath {
    param([Parameter(Mandatory)]$Profile, [Parameter(Mandatory)][string]$Path)
    if (-not ($Profile.shared -and $Profile.shared.enabled)) { return }
    $bundle = [System.IO.Path]::GetFullPath($script:LdBundle).TrimEnd('\') + '\'
    $target = [System.IO.Path]::GetFullPath($Path)
    if (-not $target.StartsWith($bundle, [StringComparison]::OrdinalIgnoreCase)) { return }
    if (-not $env:LDP_INSTANCE_ROOT) { throw "shared-release writes inside the README folder require a device workspace" }
    $instance = [System.IO.Path]::GetFullPath($env:LDP_INSTANCE_ROOT).TrimEnd('\') + '\'
    $allowed = @('manifest','logs','references','cut-pad-images') | ForEach-Object {
        [System.IO.Path]::GetFullPath((Join-Path $instance $_)).TrimEnd('\') + '\'
    }
    foreach ($root in $allowed) {
        if ($target.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { return }
    }
    throw "shared mode forbids writing to the release tree: $target; use the active instance's manifest/logs/references/cut-pad-images folder or a local output root"
}

function Set-LdEpisodeWorkspace {
    param([Parameter(Mandatory)]$Profile, [Parameter(Mandatory)][string]$Code)
    $pythonValue = [string]$Profile.python.interpreter
    if (-not $pythonValue) { $pythonValue = [string]$Profile.tools.python }
    if (-not $pythonValue) { $pythonValue = 'python' }
    $python = $null
    if (Test-Path -LiteralPath $pythonValue -PathType Leaf) {
        $python = [System.IO.Path]::GetFullPath($pythonValue)
    } else {
        $command = Get-Command -Name $pythonValue -ErrorAction SilentlyContinue
        if ($command) { $python = $command.Source }
    }
    if (-not $python) { throw "cannot prepare the non-gating episode workspace: Python executable not found: $pythonValue" }
    $helper = Join-Path $script:LdBundle 'scripts\shared_workspace.py'
    $arguments = @($helper, 'ensure', '--profile', $script:LdProfilePath, '--episode', $Code)
    if ($env:LDP_INSTANCE_ROOT) { $arguments += @('--instance-root', $env:LDP_INSTANCE_ROOT) }
    $output = @(& $python @arguments 2>&1)
    $rc = $LASTEXITCODE
    if ($rc -ne 0) { throw "workspace setup helper failed (exit $rc): $($output -join ' ')" }
    try { $result = ($output -join "`n") | ConvertFrom-Json }
    catch { throw "workspace setup helper returned invalid JSON: $($_.Exception.Message)" }
    if (-not $result.workspace.shell_exports) { throw 'workspace setup helper returned no shell exports' }
    foreach ($property in $result.workspace.shell_exports.PSObject.Properties) {
        [Environment]::SetEnvironmentVariable($property.Name, [string]$property.Value, 'Process')
    }
    if ($result.warning) { Write-Warning ([string]$result.warning) }
}

function Protect-LdSharedText {
    param([Parameter(Mandatory)]$Profile, [AllowNull()][string]$Text)
    if ($null -eq $Text -or -not ($Profile.shared -and $Profile.shared.enabled)) { return $Text }
    $release = [System.IO.Path]::GetFullPath([string]$Profile.shared.release_root).TrimEnd('\') + '\'
    if ($env:LDP_PROFILE) { $Text = $Text -replace [regex]::Escape($env:LDP_PROFILE), '<device-local-profile>' }
    if ($env:USERPROFILE) { $Text = $Text -replace [regex]::Escape($env:USERPROFILE), '<user-home>' }
    foreach ($section in @('tools','roots')) {
        $values = $Profile.$section
        if (-not $values) { continue }
        foreach ($property in $values.PSObject.Properties) {
            $value = [string]$property.Value
            if (-not $value -or -not [System.IO.Path]::IsPathRooted($value)) { continue }
            $full = [System.IO.Path]::GetFullPath($value)
            if ($full.StartsWith($release, [StringComparison]::OrdinalIgnoreCase)) { continue }
            $Text = $Text -replace [regex]::Escape($value), "<$section.$($property.Name)>"
            $Text = $Text -replace [regex]::Escape($full), "<$section.$($property.Name)>"
        }
    }
    return $Text
}

function Get-LdEpisode {
    param([Parameter(Mandatory)][string]$Code)
    $profile = Get-LdProfile
    if ($profile.shared -and $profile.shared.enabled) {
        if (-not $env:LDP_INSTANCE_ROOT -or -not $env:LDP_MANIFEST_DIR) {
            Set-LdEpisodeWorkspace -Profile $profile -Code $Code
        }
        $expectedManifestDir = [System.IO.Path]::GetFullPath((Join-Path $env:LDP_INSTANCE_ROOT 'manifest'))
        if ([System.IO.Path]::GetFullPath($env:LDP_MANIFEST_DIR) -ne $expectedManifestDir) {
            Set-LdEpisodeWorkspace -Profile $profile -Code $Code
            $expectedManifestDir = [System.IO.Path]::GetFullPath((Join-Path $env:LDP_INSTANCE_ROOT 'manifest'))
            if ([System.IO.Path]::GetFullPath($env:LDP_MANIFEST_DIR) -ne $expectedManifestDir) {
                throw "workspace helper did not set LDP_MANIFEST_DIR to the active manifest folder: $expectedManifestDir"
            }
        }
        $p = Join-Path $env:LDP_MANIFEST_DIR "$Code.json"
    } else { $p = Join-Path $script:LdBundle "episodes\$Code.json" }
    if (-not (Test-Path -LiteralPath $p)) {
        throw "no manifest for $Code - create episodes\$Code.json from episodes\_template.json after measuring it"
    }
    try { $ep = Get-Content -LiteralPath $p -Raw | ConvertFrom-Json }
    catch { throw "manifest is not valid JSON: $p ($($_.Exception.Message))" }
    return $ep
}

function Resolve-LdFile {
    <# files.<key> is either {root, name} (machine-independent) or {path}. #>
    param([Parameter(Mandatory)]$Profile, [Parameter(Mandatory)]$Episode,
          [Parameter(Mandatory)][string]$Key, [switch]$Optional)
    $spec = $Episode.files.$Key
    if (-not $spec) {
        if ($Optional) { return $null }
        throw "manifest has no files.$Key - record where the episode's $Key lives"
    }
    if ($spec -is [string]) { return $spec }
    if ($spec.root) { return Join-Path (Get-LdRoot $Profile $spec.root) $spec.name }
    if ($spec.path) { return $spec.path }
    throw "files.$Key has neither root nor path"
}

function Get-LdNumber {
    <# PS5.1 JSON numbers rendered invariantly for ffmpeg/AVS arguments. #>
    param([Parameter(Mandatory)][AllowNull()]$Value, [Parameter(Mandatory)][string]$What)
    if ($null -eq $Value) { throw "$What is not recorded - measure it and record it; never guess" }
    return [string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0:0.000000}', [double]$Value)
}

function Invoke-LdFfmpeg {
    <# Run ffmpeg with stdout+stderr captured, returning the exit code reliably.
       Native stderr arrives as ErrorRecords; keep $ErrorActionPreference=Stop
       from aborting the script mid-encode. Save exact arguments separately because
       x265's quiet log level suppresses its settings banner. #>
    param([Parameter(Mandatory)][string]$Ffmpeg, [Parameter(Mandatory)][string[]]$Arguments,
          [string]$Log)
    $prof = Get-LdProfile
    if ($Log) { Assert-LdWritablePath -Profile $prof -Path $Log }
    $recordRoot = if ($Log) { Split-Path -Parent $Log } else { Get-LdRoot $prof 'logs' }
    if (-not $recordRoot) { $recordRoot = Get-LdRoot $prof 'logs' }
    $versionPath = Join-Path $script:LdBundle 'WORKFLOW_VERSION'
    if (-not (Test-Path -LiteralPath $versionPath)) { throw "workflow version marker missing: $versionPath" }
    $workflowVersion = (Get-Content -LiteralPath $versionPath -Raw).Trim()
    if ($workflowVersion -notmatch '^[1-9]\d*$') { throw "invalid workflow version marker: '$workflowVersion'" }
    New-Item -ItemType Directory -Force -Path $recordRoot | Out-Null
    $recordPath = Join-Path $recordRoot ("ffmpeg-command-{0}.json" -f [guid]::NewGuid().ToString('N'))
    Assert-LdWritablePath -Profile $prof -Path $recordPath
    $record = [ordered]@{
        workflow_version = [int]$workflowVersion
        started_utc = [DateTimeOffset]::UtcNow.ToString('o')
        executable = Protect-LdSharedText $prof $Ffmpeg
        arguments = @($Arguments | ForEach-Object { Protect-LdSharedText $prof ([string]$_) })
        output_log = Protect-LdSharedText $prof $Log
    }
    $recordJson = ConvertTo-Json -InputObject $record -Depth 5
    [System.IO.File]::WriteAllText($recordPath, $recordJson + [Environment]::NewLine,
                                   (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "FFmpeg command record: $recordPath"
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    if ($Log) {
        if ($prof.shared -and $prof.shared.enabled) {
            & $Ffmpeg @Arguments 2>&1 |
                ForEach-Object { Protect-LdSharedText $prof ($_.ToString()) } |
                Tee-Object -FilePath $Log | Select-Object -Last 6 | ForEach-Object { Write-Host $_ }
        } else {
            & $Ffmpeg @Arguments 2>&1 | Tee-Object -FilePath $Log | Select-Object -Last 6 |
                ForEach-Object { Write-Host $_ }
        }
    } else {
        & $Ffmpeg @Arguments 2>&1 | Select-Object -Last 6 | ForEach-Object { Write-Host $_ }
    }
    $rc = $LASTEXITCODE
    $ErrorActionPreference = $old
    return $rc
}
