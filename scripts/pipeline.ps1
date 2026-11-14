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
    if (-not $p) { $p = Join-Path $script:LdBundle 'machine.json' }
    if (-not (Test-Path -LiteralPath $p)) {
        $alt = Join-Path $script:LdBundle 'machine.example.json'
        if (Test-Path -LiteralPath $alt) { $p = $alt }
        else { throw "no machine profile at $p - run preflight.ps1 -Write" }
    }
    try { return Get-Content -LiteralPath $p -Raw | ConvertFrom-Json }
    catch { throw "profile is not valid JSON: $p ($($_.Exception.Message))" }
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
    $v = $Profile.roots.$Name
    if (-not $v) { throw "root '$Name' is not set in the profile" }
    return $v
}

function Get-LdEpisode {
    param([Parameter(Mandatory)][string]$Code)
    $p = Join-Path $script:LdBundle "episodes\$Code.json"
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
    $recordRoot = if ($Log) { Split-Path -Parent $Log } else { Get-LdRoot (Get-LdProfile) 'logs' }
    if (-not $recordRoot) { $recordRoot = Get-LdRoot (Get-LdProfile) 'logs' }
    $versionPath = Join-Path $script:LdBundle 'WORKFLOW_VERSION'
    if (-not (Test-Path -LiteralPath $versionPath)) { throw "workflow version marker missing: $versionPath" }
    $workflowVersion = (Get-Content -LiteralPath $versionPath -Raw).Trim()
    if ($workflowVersion -notmatch '^[1-9]\d*$') { throw "invalid workflow version marker: '$workflowVersion'" }
    New-Item -ItemType Directory -Force -Path $recordRoot | Out-Null
    $recordPath = Join-Path $recordRoot ("ffmpeg-command-{0}.json" -f [guid]::NewGuid().ToString('N'))
    $record = [ordered]@{
        workflow_version = [int]$workflowVersion
        started_utc = [DateTimeOffset]::UtcNow.ToString('o')
        executable = $Ffmpeg
        arguments = @($Arguments)
        output_log = $Log
    }
    $recordJson = ConvertTo-Json -InputObject $record -Depth 5
    [System.IO.File]::WriteAllText($recordPath, $recordJson + [Environment]::NewLine,
                                   (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "FFmpeg command record: $recordPath"
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    if ($Log) {
        & $Ffmpeg @Arguments 2>&1 | Tee-Object -FilePath $Log | Select-Object -Last 6 |
            ForEach-Object { Write-Host $_ }
    } else {
        & $Ffmpeg @Arguments 2>&1 | Select-Object -Last 6 | ForEach-Object { Write-Host $_ }
    }
    $rc = $LASTEXITCODE
    $ErrorActionPreference = $old
    return $rc
}
