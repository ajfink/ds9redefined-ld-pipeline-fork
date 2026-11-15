<#
.SYNOPSIS
  Resumable, metadata-verified downloader for one Internet Archive LaserDisc item.

.DESCRIPTION
  Downloads only TBCs, matching .tbc.json sidecars, and raw .efm/.efm.7z files.
  It refuses non-Archive URLs, validates the item metadata before downloading,
  resumes partial files, and verifies size + MD5. It never deletes or replaces
  an existing file. If a complete-sized file has a bad hash, stop and inspect it.
  -ListOnly prints the complete capture inventory, aggregate byte count, and projected
  free-space gate. A real download will not start unless that gate passes.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\download_archive.ps1 `
    -Url https://archive.org/details/DS9-S01E05-LaserDisc-Copies `
    -Destination 'D:\DS9 S01E05 LaserDisc'

.EXAMPLE
  # Inspect the exact file set and Archive metadata without downloading:
  powershell -File scripts\download_archive.ps1 -Url <archive-url> `
    -Destination <folder> -ListOnly
#>
param(
    [Parameter(Mandatory)][string]$Url,
    [Parameter(Mandatory)][string]$Destination,
    [switch]$ListOnly
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'pipeline.ps1')
$profile = Get-LdProfile
$curl = Get-LdTool $profile 'curl'
$curlVersionOutput = @(& $curl --version 2>&1)
if ($LASTEXITCODE -ne 0) { throw "Profile curl failed its version check: $curl ($($curlVersionOutput -join ' '))" }
$curlVersionMatch = [regex]::Match(($curlVersionOutput | Select-Object -First 1), '(?i)\bcurl(?:/|\s+)(\d+\.\d+\.\d+)')
if (-not $curlVersionMatch.Success) { throw "Could not parse the profile curl version from: $($curlVersionOutput -join ' ')" }
$curlVersion = [version]$curlVersionMatch.Groups[1].Value
if ($curlVersion -lt [version]'7.71.0') {
    throw "Download requires curl 7.71.0 or newer for --retry-all-errors; found $curlVersion at $curl"
}
Write-Host "Downloader curl : $curlVersion (minimum 7.71.0)"

function Get-ArchiveIdentifier([string]$Value) {
    try { $u = [Uri]$Value } catch { throw "Invalid URL: $Value" }
    if ($u.Scheme -ne 'https' -or $u.DnsSafeHost -notin @('archive.org', 'www.archive.org')) {
        throw "Only an https://archive.org URL is accepted: $Value"
    }
    $parts = @($u.AbsolutePath.Trim('/') -split '/')
    if ($parts.Count -lt 2 -or $parts[0] -notin @('details', 'download')) {
        throw "Expected an Archive item URL of the form /details/<identifier> or /download/<identifier>."
    }
    $id = [Uri]::UnescapeDataString($parts[1])
    if ($id -notmatch '^[A-Za-z0-9._-]+$') { throw "Unexpected Archive identifier: $id" }
    return $id
}

$identifier = Get-ArchiveIdentifier $Url
$metadataUrl = "https://archive.org/metadata/$identifier"
Write-Host "Archive item : $identifier"
Write-Host "Metadata URL : $metadataUrl"

try { $metadata = Invoke-RestMethod -Uri $metadataUrl -Method Get -TimeoutSec 90 }
catch { throw "Could not read Archive metadata for '$identifier': $($_.Exception.Message)" }
if (-not $metadata.metadata -or -not $metadata.files) {
    throw "Archive metadata response has no item/file list for '$identifier'."
}
$itemTitle = [string]$metadata.metadata.title
if (-not $itemTitle) { $itemTitle = $identifier }

$selected = @($metadata.files | Where-Object {
    $_.name -match '(?i)\.tbc(?:\.json)?$|\.efm(?:\.7z)?$'
})
if (-not $selected) { throw "No .tbc/.tbc.json/raw EFM inputs found in Archive item '$identifier'." }
foreach ($f in $selected) {
    if (-not $f.name -or [IO.Path]::GetFileName([string]$f.name) -ne [string]$f.name) {
        throw "Unsafe Archive filename in metadata: '$($f.name)'"
    }
    if ([string]$f.size -notmatch '^\d+$' -or [string]$f.md5 -notmatch '^[0-9a-fA-F]{32}$') {
        throw "Archive metadata lacks a valid size/MD5 for '$($f.name)'; refusing an unverified download."
    }
}
$names = @($selected | ForEach-Object { [string]$_.name })
$missingJson = @($selected | Where-Object { $_.name -match '(?i)\.tbc$' } |
    ForEach-Object { "$($_.name).json" } | Where-Object { $_ -notin $names })
if ($missingJson) {
    throw "TBCs without paired JSON sidecars: $($missingJson -join ', '). Do not build a stack from an incomplete inventory."
}

Write-Host "Item title   : $itemTitle"
$tbcFiles = @($selected | Where-Object { $_.name -match '(?i)\.tbc$' })
$captureLabels = @($tbcFiles | ForEach-Object {
    $stem = [IO.Path]::GetFileNameWithoutExtension([string]$_.name)
    ($stem -split '-')[-1]
} | Sort-Object -Unique)
[long]$selectedBytes = 0
foreach ($f in $selected) { $selectedBytes += [long]$f.size }
Write-Host "Selected     : $($selected.Count) files ($($tbcFiles.Count) TBCs; captures: $($captureLabels -join ', '))"
Write-Host ("Expected total: {0:N0} bytes ({1:N1} GB decimal / {2:N1} GiB)" -f $selectedBytes, ($selectedBytes / 1e9), ($selectedBytes / 1GB))
$destinationFullPath = [IO.Path]::GetFullPath($Destination)
Assert-LdWritablePath -Profile $profile -Path $destinationFullPath
[long]$additionalBytes = 0
foreach ($f in $selected) {
    $expected = [long]$f.size
    $target = Join-Path $destinationFullPath ([string]$f.name)
    if (Test-Path -LiteralPath $target) {
        if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
            throw "Destination entry exists but is not a file; inspect without modifying it: $target"
        }
        $existingBytes = [long](Get-Item -LiteralPath $target).Length
        if ($existingBytes -gt $expected) {
            throw "Existing file is larger than Archive metadata; leaving it untouched: $target"
        }
        $additionalBytes += ($expected - $existingBytes)
    } else {
        $additionalBytes += $expected
    }
}
$reserveGiB = 80.0
if ($profile.limits -and $null -ne $profile.limits.min_free_gb) {
    $reserveGiB = [double]$profile.limits.min_free_gb
}
$destinationRoot = [IO.Path]::GetPathRoot($destinationFullPath)
$driveName = $destinationRoot.TrimEnd('\').TrimEnd(':')
$spaceStatus = 'UNKNOWN'
$currentFree = $null
try {
    # DriveInfo handles both Windows drive roots (for example C:\) and POSIX
    # volume roots (/). Get-PSDrive below remains a Windows fallback.
    $destinationDriveInfo = [System.IO.DriveInfo]::new($destinationRoot)
    if ($destinationDriveInfo.IsReady) {
        $currentFree = [long]$destinationDriveInfo.AvailableFreeSpace
    }
} catch {
    # Keep the Windows provider fallback below; report UNKNOWN if both fail.
}
if ($null -eq $currentFree -and $driveName -match '^[A-Za-z]$') {
    $destinationDrive = Get-PSDrive -Name $driveName -PSProvider FileSystem -ErrorAction SilentlyContinue
    if ($destinationDrive -and $null -ne $destinationDrive.Free) {
        $currentFree = [long]$destinationDrive.Free
    }
}
if ($null -ne $currentFree) {
    $projectedFree = $currentFree - $additionalBytes
    $reserveBytes = [long][math]::Ceiling($reserveGiB * 1GB)
    $spaceStatus = if ($projectedFree -ge $reserveBytes) { 'PASS' } else { 'FAIL' }
    Write-Host ("Destination free: {0:N1} GiB; additional required: {1:N1} GiB; projected free: {2:N1} GiB; reserve: {3:N1} GiB; SPACE GATE {4}" -f ($currentFree / 1GB), ($additionalBytes / 1GB), ($projectedFree / 1GB), $reserveGiB, $spaceStatus)
} else {
    Write-Warning "Could not query free space for destination '$destinationFullPath'. Verify free space manually and do not start unless projected free space remains at least $reserveGiB GiB."
}
$selected | Sort-Object name | ForEach-Object {
    Write-Host ("  {0,-64} {1,14:N0} bytes  MD5 {2}" -f $_.name, [long]$_.size, ([string]$_.md5).ToLowerInvariant())
}
if ($ListOnly) {
    Write-Host 'LIST ONLY - no downloads started.'
    if ($spaceStatus -eq 'FAIL') { exit 3 }
    exit 0
}
if ($spaceStatus -ne 'PASS') {
    throw "Download not started: destination free-space gate is $spaceStatus. Resolve/check capacity before downloading."
}

New-Item -ItemType Directory -Force -Path $Destination | Out-Null
$destinationRoot = (Resolve-Path -LiteralPath $Destination).Path
$baseUrl = "https://archive.org/download/$identifier"
$results = New-Object System.Collections.Generic.List[object]

foreach ($f in ($selected | Sort-Object name)) {
    $name = [string]$f.name
    $expectedSize = [long]$f.size
    $expectedMd5 = ([string]$f.md5).ToLowerInvariant()
    $path = Join-Path $destinationRoot $name
    $verified = $false
    if (Test-Path -LiteralPath $path) {
        $existing = Get-Item -LiteralPath $path
        if ($existing.Length -gt $expectedSize) {
            throw "Existing file is larger than Archive metadata; leaving it untouched: $path"
        }
        if ($existing.Length -eq $expectedSize) {
            $hash = (Get-FileHash -LiteralPath $path -Algorithm MD5).Hash.ToLowerInvariant()
            if ($hash -ne $expectedMd5) {
                throw "Existing complete-sized file has an MD5 mismatch; leaving it untouched: $path (got $hash expected $expectedMd5)"
            }
            $verified = $true
        }
    }

    if (-not $verified) {
        $escaped = [Uri]::EscapeDataString($name)
        $fileUrl = "$baseUrl/$escaped"
        Write-Host "Download/resume: $name"
        $oldEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        & $curl --location --fail --retry 10 --retry-all-errors --retry-delay 5 `
            --connect-timeout 30 --continue-at - --output $path $fileUrl 2>&1 |
            ForEach-Object { Write-Host $_ }
        $rc = $LASTEXITCODE
        $ErrorActionPreference = $oldEap
        if ($rc -ne 0) { throw "curl failed for '$name' (exit $rc). Partial data is retained; rerun to resume." }
        $actual = Get-Item -LiteralPath $path
        if ($actual.Length -ne $expectedSize) {
            throw "Size mismatch for '$name': got $($actual.Length), expected $expectedSize. File retained for inspection."
        }
        $hash = (Get-FileHash -LiteralPath $path -Algorithm MD5).Hash.ToLowerInvariant()
        if ($hash -ne $expectedMd5) {
            throw "MD5 mismatch for '$name': got $hash, expected $expectedMd5. File retained; do not use it."
        }
    }

    $results.Add([pscustomobject]@{
        Name = $name; Bytes = $expectedSize; MD5 = $expectedMd5
        ArchiveId = $identifier; Status = 'verified'
    })
    Write-Host "VERIFIED $name"
}

$manifest = Join-Path $destinationRoot 'download-manifest.csv'
if (Test-Path -LiteralPath $manifest) {
    $prior = @(Import-Csv -LiteralPath $manifest)
    $same = $prior.Count -eq $results.Count
    foreach ($row in $prior) {
        $match = @($results | Where-Object {
            $_.ArchiveId -eq $identifier -and $_.Name -eq $row.Name -and
            [long]$_.Bytes -eq [long]$row.Bytes -and $_.MD5 -eq $row.MD5
        })
        if ($match.Count -ne 1) { $same = $false; break }
    }
    if (-not $same) { throw "Existing download manifest differs; preserving it and refusing to overwrite: $manifest" }
    Write-Host "Existing manifest matches this Archive item; preserved: $manifest"
} else {
    $results | Export-Csv -LiteralPath $manifest -NoTypeInformation -Encoding UTF8
}
$readme = Join-Path $destinationRoot 'SOURCE-ITEM.txt'
if (Test-Path -LiteralPath $readme) {
    $priorText = Get-Content -LiteralPath $readme -Raw -Encoding UTF8
    if ($priorText -notmatch "(?m)^Archive item: $([regex]::Escape($identifier))$") {
        throw "Existing source note belongs to another item; preserving it and refusing to overwrite: $readme"
    }
    Write-Host "Existing source note belongs to this item; preserved: $readme"
} else {
    @("Archive item: $identifier", "Title: $itemTitle", "Source URL: $Url",
      "Downloaded file types: .tbc, .tbc.json, .efm/.efm.7z only.",
      "Manifest: download-manifest.csv", "Verified files: $($results.Count)") |
        Set-Content -LiteralPath $readme -Encoding UTF8
}
Write-Host "ALL $($results.Count) selected files verified. Manifest: $manifest"
