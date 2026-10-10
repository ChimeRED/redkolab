<#
.SYNOPSIS
    Copy the freshly built package into the local Blender add-on zip library, replacing older versions.

.DESCRIPTION
    The local add-on zip library collects installable add-on zips (Boxcutter, HardOps,
    MACHIN3tools, ...). Every REDKO_LAB release should land there too, so this script
    copies dist/redkolab-<version>.zip into the target folder and deletes older
    redkolab-*.zip files from it.

    Safety rules (this script touches files outside the repository):
      * the target folder must already exist - it is never created by guessing a path
      * only files matching redkolab-*.zip are ever deleted, and only when they sit
        directly inside the resolved target folder
      * -KeepOld keeps previous versions around instead of replacing them

    ASCII-only on purpose: Windows PowerShell 5.1 reads .ps1 files with the system
    code page and would mangle non-ASCII text.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\deploy_local.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\deploy_local.ps1 -Target "D:\other\addons" -KeepOld
#>
param(
    [string]$Zip = "",
    [string]$Target = "D:\build\Blender\4.0\4.0chajian",
    [switch]$KeepOld,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot

function Write-Info($message, $color) {
    if (-not $Quiet) {
        if ($color) { Write-Host $message -ForegroundColor $color } else { Write-Host $message }
    }
}

# --- which package ----------------------------------------------------------
if ([string]::IsNullOrWhiteSpace($Zip)) {
    $package = Get-ChildItem -LiteralPath (Join-Path $projectRoot "dist") -Filter "redkolab-*.zip" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $package) { throw "no redkolab-*.zip in dist - run tools/build_zips.py first" }
} else {
    if (-not (Test-Path -LiteralPath $Zip)) { throw "package not found: $Zip" }
    $package = Get-Item -LiteralPath $Zip
}

# --- which folder -----------------------------------------------------------
if (-not (Test-Path -LiteralPath $Target)) {
    throw "target folder not found: $Target (create it first or pass -Target)"
}
$targetFull = (Resolve-Path -LiteralPath $Target).Path

Write-Info "package : $($package.FullName)"
Write-Info "target  : $targetFull"

# --- replace older versions -------------------------------------------------
$pattern = "redkolab-*.zip"
$existing = @(Get-ChildItem -LiteralPath $targetFull -Filter $pattern -File -ErrorAction SilentlyContinue)

foreach ($file in $existing) {
    if ($file.FullName -eq $package.FullName) { continue }
    $parent = Split-Path -Parent $file.FullName
    if ($parent -ne $targetFull) { continue }   # never delete outside the target folder
    if ($KeepOld) {
        Write-Info "keep    : $($file.Name)"
        continue
    }
    Write-Info "replace : $($file.Name)" "Yellow"
    Remove-Item -LiteralPath $file.FullName -Force
}

# --- copy the new one -------------------------------------------------------
$destination = Join-Path $targetFull $package.Name
if ($destination -ne $package.FullName) {
    Copy-Item -LiteralPath $package.FullName -Destination $destination -Force
}
Write-Info "copied  : $($package.Name)" "Green"

$after = @(Get-ChildItem -LiteralPath $targetFull -Filter $pattern -File)
Write-Info "in target now: $($after.Name -join ', ')"
