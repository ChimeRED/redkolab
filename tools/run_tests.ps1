<#
.SYNOPSIS
    Run the REDKOlab (红红子lab) real-Blender test suite against a given Blender build.

.DESCRIPTION
    Installs dist/redkolab-<version>.zip into an isolated Blender configuration
    (temporary config/scripts/extensions directories), enables the add-on, then runs
    the functional assertions in tools/blender_test.py. Your real Blender
    configuration is never touched.

    -Mode auto (default) lets the test script decide how to install: Blender 4.2+
    installs the same zip as an extension (it finds blender_manifest.toml at the
    archive root), 4.0/4.1 installs it as a classic add-on (it finds the redkolab/
    directory).

    This file is intentionally ASCII-only: Windows PowerShell 5.1 reads .ps1 files
    using the system code page, which mangles non-ASCII text without a BOM.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\run_tests.ps1 -Blender "D:\build\Blender\5.0\blender.exe"

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\run_tests.ps1 -Blender "D:\Steam\steamapps\common\Blender\blender.exe" -Mode addon_install -Zip dist\redkolab-1.1.0-hybrid.zip
#>
param(
    [Parameter(Mandatory = $true)][string]$Blender,
    [ValidateSet("auto", "addon_install", "extensions", "legacy", "extension")][string]$Mode = "auto",
    [string]$Zip = "",
    [string]$WorkDir = (Join-Path $env:TEMP "redkolab_tests"),
    [switch]$KeepEnv
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$distDir = Join-Path $projectRoot "dist"
$testScript = Join-Path $PSScriptRoot "blender_test.py"

if (-not (Test-Path -LiteralPath $Blender)) {
    throw "Blender not found: $Blender"
}

# --- version ----------------------------------------------------------------
# Some builds (e.g. the Steam build of 4.0.2) print a TBBmalloc warning to stderr
# on startup; with ErrorActionPreference = "Stop" that would abort the script, so
# relax it around every native Blender call.
$ErrorActionPreference = "Continue"
$versionLines = & $Blender --version 2>&1
$ErrorActionPreference = "Stop"

$versionOutput = $versionLines | Where-Object { $_ -match "Blender" } | Select-Object -First 1
if ($versionOutput -notmatch "(\d+)\.(\d+)\.(\d+)") {
    throw "Cannot detect the Blender version from: $versionLines"
}
$major = [int]$Matches[1]
$minor = [int]$Matches[2]
$versionText = "$($Matches[1]).$($Matches[2]).$($Matches[3])"
$tag = "blender_$($major)_$($minor)"

# --- package ----------------------------------------------------------------
if ([string]::IsNullOrWhiteSpace($Zip)) {
    $package = Get-ChildItem -LiteralPath $distDir -Filter "redkolab-*.zip" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $package) {
        throw "No redkolab-*.zip in $distDir - run tools/build_zips.py first."
    }
} else {
    $package = Get-Item -LiteralPath $Zip
}
$zipPath = $package.FullName

# --- isolated config dirs ---------------------------------------------------
$envConfig = Join-Path $WorkDir "$tag\config"
$envScripts = Join-Path $WorkDir "$tag\scripts"
$envExtensions = Join-Path $WorkDir "$tag\extensions"
foreach ($dir in @($envConfig, $envScripts, $envExtensions)) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

Write-Host "Blender $versionText | mode: $Mode | package: $($package.Name)" -ForegroundColor Cyan
Write-Host "isolated config: $envConfig"

$env:BLENDER_USER_CONFIG = $envConfig
$env:BLENDER_USER_SCRIPTS = $envScripts
$env:BLENDER_USER_EXTENSIONS = $envExtensions

# Blender writes its Python tracebacks to stderr; redirect everything to a log
# file (redirecting to the pipeline instead would raise NativeCommandError).
$logFile = Join-Path $WorkDir "$tag\test_output.log"
$ErrorActionPreference = "Continue"
& $Blender --background --factory-startup --python-exit-code 3 `
    --python $testScript -- --zip $zipPath --mode $Mode *> $logFile
$code = $LASTEXITCODE
$ErrorActionPreference = "Stop"
Get-Content -LiteralPath $logFile | Where-Object { $_ -notmatch "TBBmalloc" } | Write-Host

if (-not $KeepEnv) {
    Remove-Item Env:\BLENDER_USER_CONFIG -ErrorAction SilentlyContinue
    Remove-Item Env:\BLENDER_USER_SCRIPTS -ErrorAction SilentlyContinue
    Remove-Item Env:\BLENDER_USER_EXTENSIONS -ErrorAction SilentlyContinue
}

if ($code -eq 0) {
    Write-Host ""
    Write-Host "PASS - Blender $versionText" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "FAIL - Blender $versionText (exit=$code)" -ForegroundColor Red
}
exit $code
