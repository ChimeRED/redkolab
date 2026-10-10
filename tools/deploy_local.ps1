<#
.SYNOPSIS
    Deploy a freshly built REDKO_LAB package: local add-on zip library and/or an installed add-on folder.

.DESCRIPTION
    Two independent targets:

      1. Zip library (-Target, default D:\build\Blender\4.0\4.0chajian)
         Copies dist/redkolab-<version>.zip there and deletes older redkolab-*.zip
         from it, so the library always holds exactly one current version.

      2. Installed add-on folder (-InstallTo, optional)
         Replaces <InstallTo>\redkolab with the contents of the zip, so the copy
         Blender actually loads is up to date. Blender must be restarted (or the
         add-on re-enabled) to pick up the new files.

    Safety rules (this script touches files outside the repository):
      * the target / addons folder must already exist - paths are never guessed into existence
      * only files matching redkolab-*.zip are deleted from the zip library
      * the installed folder is only replaced when it resolves to <addons>\redkolab
      * -KeepOld keeps previous zip versions around

    ASCII-only on purpose: Windows PowerShell 5.1 reads .ps1 files with the system
    code page and would mangle non-ASCII text.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\deploy_local.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\deploy_local.ps1 `
        -InstallTo "$env:APPDATA\Blender Foundation\Blender\4.0\scripts\addons"
#>
param(
    [string]$Zip = "",
    [string]$Target = "D:\build\Blender\4.0\4.0chajian",
    [string]$InstallTo = "",
    [switch]$KeepOld,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression.FileSystem

$projectRoot = Split-Path -Parent $PSScriptRoot
$moduleName = "redkolab"

function Write-Info($message, $color) {
    if (-not $Quiet) {
        if ($color) { Write-Host $message -ForegroundColor $color } else { Write-Host $message }
    }
}

# --- which package ----------------------------------------------------------
if ([string]::IsNullOrWhiteSpace($Zip)) {
    $package = Get-ChildItem -LiteralPath (Join-Path $projectRoot "dist") -Filter "$moduleName-*.zip" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $package) { throw "no $moduleName-*.zip in dist - run tools/build_zips.py first" }
} else {
    if (-not (Test-Path -LiteralPath $Zip)) { throw "package not found: $Zip" }
    $package = Get-Item -LiteralPath $Zip
}
Write-Info "package : $($package.FullName)"

# --- 1. zip library ---------------------------------------------------------
if (-not [string]::IsNullOrWhiteSpace($Target)) {
    if (-not (Test-Path -LiteralPath $Target)) {
        throw "zip library folder not found: $Target (create it first or pass -Target '')"
    }
    $targetFull = (Resolve-Path -LiteralPath $Target).Path
    Write-Info "library : $targetFull"

    $pattern = "$moduleName-*.zip"
    $existing = @(Get-ChildItem -LiteralPath $targetFull -Filter $pattern -File -ErrorAction SilentlyContinue)
    foreach ($file in $existing) {
        if ($file.FullName -eq $package.FullName) { continue }
        if ((Split-Path -Parent $file.FullName) -ne $targetFull) { continue }
        if ($KeepOld) {
            Write-Info "keep    : $($file.Name)"
            continue
        }
        Write-Info "replace : $($file.Name)" "Yellow"
        Remove-Item -LiteralPath $file.FullName -Force
    }

    $destination = Join-Path $targetFull $package.Name
    if ($destination -ne $package.FullName) {
        Copy-Item -LiteralPath $package.FullName -Destination $destination -Force
    }
    Write-Info "copied  : $($package.Name)" "Green"
    $after = @(Get-ChildItem -LiteralPath $targetFull -Filter $pattern -File)
    Write-Info "library now: $($after.Name -join ', ')"
}

# --- 2. installed add-on folder ---------------------------------------------
if (-not [string]::IsNullOrWhiteSpace($InstallTo)) {
    if (-not (Test-Path -LiteralPath $InstallTo)) {
        throw "addons folder not found: $InstallTo"
    }
    $addonsFull = (Resolve-Path -LiteralPath $InstallTo).Path
    $installDir = Join-Path $addonsFull $moduleName

    # Safety: only ever touch <addons>\redkolab
    if ((Split-Path -Leaf $installDir) -ne $moduleName) {
        throw "refusing to touch $installDir (expected a folder named $moduleName)"
    }
    if (-not (Test-Path -LiteralPath $installDir)) {
        Write-Info "install : skipped, $installDir does not exist (install the add-on once first)" "Yellow"
    } else {
        Write-Info "install : $installDir"
        Remove-Item -LiteralPath $installDir -Recurse -Force
        New-Item -ItemType Directory -Force -Path $installDir | Out-Null

        $archive = [System.IO.Compression.ZipFile]::OpenRead($package.FullName)
        try {
            $prefix = "$moduleName/"
            $copied = 0
            foreach ($entry in $archive.Entries) {
                if (-not $entry.FullName.StartsWith($prefix)) { continue }
                if ([string]::IsNullOrEmpty($entry.Name)) { continue }
                $relative = $entry.FullName.Substring($prefix.Length)
                $outPath = Join-Path $installDir $relative
                $outDir = Split-Path -Parent $outPath
                if (-not (Test-Path -LiteralPath $outDir)) { New-Item -ItemType Directory -Force -Path $outDir | Out-Null }
                [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $outPath, $true)
                $copied++
            }
        } finally {
            $archive.Dispose()
        }
        Write-Info "installed: $copied files -> $installDir" "Green"
        Write-Info "note    : restart Blender (or disable/enable the add-on) to load the new files" "Yellow"
    }
}
