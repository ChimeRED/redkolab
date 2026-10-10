<#
.SYNOPSIS
    Verify, tag and push a REDKOlab release.

.DESCRIPTION
    Pushing the tag triggers .github/workflows/release.yml, which builds the
    package, self-checks its structure, runs the real Blender test suite on
    Blender 4.2 / 4.5 / 5.2, then publishes and archives the zip in GitHub
    Releases.

    Steps performed here (before anything is pushed):
      1. git working tree must be clean
      2. optional version bump (bl_info + blender_manifest.toml, always in sync)
      3. version consistency check (tools/check_version.py)
      4. build the package (tools/build_zips.py)
      5. package self-check (tools/inspect_zip.py)
      6. real-Blender gate (tools/run_tests.ps1, default 4.0.2 / auto) - the
         release is aborted here if the add-on cannot be installed, enabled and
         pass its assertions, so a broken package can never reach a tag
      7. commit, tag and push (with retries)

    NOTE: this file is intentionally ASCII-only. Windows PowerShell 5.1 reads
    .ps1 files using the system code page, which mangles non-ASCII text and can
    even break parsing.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\release.ps1
    powershell -ExecutionPolicy Bypass -File tools\release.ps1 -Bump patch
    powershell -ExecutionPolicy Bypass -File tools\release.ps1 -Bump minor -DryRun
    powershell -ExecutionPolicy Bypass -File tools\release.ps1 -SkipRealTest
#>
param(
    [ValidateSet("none", "patch", "minor", "major")][string]$Bump = "none",
    [string]$Remote = "origin",
    [string]$Git = "",
    [string]$Python = "",
    # Local add-on zip library: every release is copied there, replacing older versions
    [string]$DeployTo = "D:\build\Blender\4.0\4.0chajian",
    # Installed add-on folder: keeps the copy Blender actually loads up to date
    [string]$InstallTo = "$env:APPDATA\Blender Foundation\Blender\4.0\scripts\addons",
    # Real-Blender gate: runs before anything is committed, tagged or pushed.
    # Defaults to the widest-coverage combo (4.0.2 supports both install entries).
    [string]$RealTestBlender = "D:\SteamLibrary\steamapps\common\Blender\blender.exe",
    [ValidateSet("auto", "extensions")][string]$RealTestMode = "auto",
    [switch]$SkipRealTest,
    [switch]$NoDeploy,
    [switch]$NoInstall,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

$portableRoot = Join-Path $env:LOCALAPPDATA "redkolab-tools"

function Resolve-Tool {
    param(
        [string]$Explicit,
        [string[]]$Names,
        [string[]]$HintFiles,
        [string[]]$Globs
    )
    if ($Explicit) { return $Explicit }
    # Hint files first: they are written by an explicit setup step and mean
    # "use exactly this one". On Windows a bare "py" may resolve to the
    # Microsoft Store stub, which returns non-zero without doing anything.
    foreach ($file in $HintFiles) {
        if (Test-Path -LiteralPath $file) {
            $path = (Get-Content -LiteralPath $file -Raw).Trim()
            if ($path -and (Test-Path -LiteralPath $path)) { return $path }
        }
    }
    foreach ($name in $Names) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) { return $command.Source }
    }
    foreach ($pattern in $Globs) {
        $hit = Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

$gitExe = Resolve-Tool -Explicit $Git -Names @("git") `
    -HintFiles @((Join-Path $portableRoot "git_path.txt")) `
    -Globs @("C:\Program Files\Git\cmd\git.exe", "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe")
if (-not $gitExe) { throw "git not found - pass -Git <path to git.exe>" }

$pythonExe = Resolve-Tool -Explicit $Python -Names @("python", "py") `
    -HintFiles @((Join-Path $portableRoot "python_path.txt")) `
    -Globs @("$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "C:\Python3*\python.exe")
if (-not $pythonExe) { throw "python not found - pass -Python <path to python.exe>" }

$packageDir = Join-Path $projectRoot "redkolab"
$initPath = Join-Path $packageDir "__init__.py"
$manifestPath = Join-Path $packageDir "blender_manifest.toml"

function Get-CurrentVersion {
    $text = Get-Content -LiteralPath $initPath -Raw -Encoding UTF8
    $match = [regex]::Match($text, '"version"\s*:\s*\((\d+)\s*,\s*(\d+)\s*,\s*(\d+)\)')
    if (-not $match.Success) { throw "bl_info version not found in __init__.py" }
    return @([int]$match.Groups[1].Value, [int]$match.Groups[2].Value, [int]$match.Groups[3].Value)
}

function Set-Version {
    param([int[]]$Parts)
    $text = Get-Content -LiteralPath $initPath -Raw -Encoding UTF8
    $text = [regex]::Replace($text, '("version"\s*:\s*\()\d+\s*,\s*\d+\s*,\s*\d+(\))',
        ('${1}' + $Parts[0] + ', ' + $Parts[1] + ', ' + $Parts[2] + '${2}'))
    # Must be BOM-free UTF-8. "Set-Content -Encoding UTF8" writes EF BB BF on
    # Windows PowerShell 5.1, and that BOM makes ast.parse in check_version.py /
    # build_zips.py die with "invalid non-printable character U+FEFF", i.e. the
    # -Bump release path would break itself.
    [System.IO.File]::WriteAllText($initPath, $text, (New-Object System.Text.UTF8Encoding($false)))

    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8
    $manifest = [regex]::Replace($manifest, '(?m)^version\s*=\s*"[^"]*"',
        ('version = "' + ($Parts -join '.') + '"'))
    [System.IO.File]::WriteAllText($manifestPath, $manifest, (New-Object System.Text.UTF8Encoding($false)))
}

function Invoke-GitPush {
    # The first connection attempt to github.com intermittently times out on some
    # networks (~21s) and succeeds on retry, so be patient.
    param([string]$Reference, [int]$Attempts = 6)
    for ($i = 1; $i -le $Attempts; $i++) {
        & $gitExe push $Remote $Reference
        if ($LASTEXITCODE -eq 0) { return }
        Write-Host "push $Reference failed (attempt $i/$Attempts), retrying in 15s ..." -ForegroundColor Yellow
        Start-Sleep -Seconds 15
    }
    throw "git push $Reference failed after $Attempts attempts"
}

# --- 1. clean tree ----------------------------------------------------------
$status = & $gitExe status --porcelain
if ($status) {
    Write-Host "working tree is not clean:" -ForegroundColor Yellow
    & $gitExe status --short
    throw "git working tree is not clean"
}

# --- 2. optional version bump ----------------------------------------------
$version = Get-CurrentVersion
if ($Bump -ne "none") {
    switch ($Bump) {
        "major" { $version[0]++; $version[1] = 0; $version[2] = 0 }
        "minor" { $version[1]++; $version[2] = 0 }
        "patch" { $version[2]++ }
    }
    Set-Version -Parts $version
    Write-Host ("version bumped to " + ($version -join ".")) -ForegroundColor Cyan
}
$versionText = ($version -join ".")
$tag = "v$versionText"

# --- 3. checks --------------------------------------------------------------
& $pythonExe (Join-Path $PSScriptRoot "check_version.py") $tag
if ($LASTEXITCODE -ne 0) { throw "version consistency check failed" }

& $pythonExe (Join-Path $PSScriptRoot "build_zips.py")
if ($LASTEXITCODE -ne 0) { throw "packaging failed" }

$zips = Get-ChildItem -LiteralPath (Join-Path $projectRoot "dist") -Filter "*.zip"
& $pythonExe (Join-Path $PSScriptRoot "inspect_zip.py") $zips.FullName
if ($LASTEXITCODE -ne 0) { throw "package self-check failed - release aborted" }

# The artifact that gets gated, deployed and published: the newest zip, i.e. the
# one build_zips.py just wrote for the version check_version.py just approved.
$newestZip = ($zips | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName

# --- 4. real-Blender gate ---------------------------------------------------
# inspect_zip.py only checks the structure of the archive; it never enables the
# add-on, so a structurally valid zip can still be a package that no Blender
# version can load. That is how a dead build got tagged once, so tagging now
# requires one real install + enable + full assertion run on the exact zip that
# is about to be released. Skipping it is possible but never the default.
if ($SkipRealTest) {
    Write-Host "real-Blender gate skipped (-SkipRealTest)" -ForegroundColor Yellow
} else {
    if (-not (Test-Path -LiteralPath $RealTestBlender)) {
        throw "Blender not found for the release gate: $RealTestBlender (pass -RealTestBlender, or -SkipRealTest to bypass)"
    }
    Write-Host ("real-Blender gate: {0} (mode {1})" -f $RealTestBlender, $RealTestMode) -ForegroundColor Cyan
    # Native Blender prints warnings to stderr; keep those from becoming
    # terminating errors here and judge by the exit code alone.
    $ErrorActionPreference = "Continue"
    & (Join-Path $PSScriptRoot "run_tests.ps1") -Blender $RealTestBlender -Mode $RealTestMode -Zip $newestZip
    $gateCode = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($gateCode -ne 0) {
        throw "real-Blender gate failed (exit=$gateCode) - release aborted, nothing committed, tagged or pushed"
    }
}

# --- 5. local add-on zip library -------------------------------------------
# Do this before pushing: the local library gets the new zip even if the network
# is down and the push has to be retried later.
$installTarget = ""
if (-not $NoInstall) { $installTarget = $InstallTo }
if ($DryRun) {
    Write-Host "DryRun: would copy $([System.IO.Path]::GetFileName($newestZip)) to $DeployTo" -ForegroundColor Yellow
    if ($installTarget) { Write-Host "DryRun: would refresh the installed add-on in $installTarget" -ForegroundColor Yellow }
} elseif ($NoDeploy -and -not $installTarget) {
    Write-Host "local deploy skipped"
} else {
    $deployArgs = @{ Zip = $newestZip }
    if ($NoDeploy) { $deployArgs.Target = "" } else { $deployArgs.Target = $DeployTo }
    if ($installTarget) { $deployArgs.InstallTo = $installTarget }
    & (Join-Path $PSScriptRoot "deploy_local.ps1") @deployArgs
}

# --- 6. commit / tag / push -------------------------------------------------
$branch = (& $gitExe rev-parse --abbrev-ref HEAD).Trim()

if ($DryRun) {
    Write-Host ""
    Write-Host "DryRun: all checks passed, nothing was committed or pushed." -ForegroundColor Yellow
    Write-Host "Would run:"
    Write-Host "  git add -A"
    Write-Host "  git commit -m `"release: $tag`""
    Write-Host "  git tag -a $tag"
    Write-Host "  git push $Remote $branch"
    Write-Host "  git push $Remote $tag"
    exit 0
}

if ($Bump -ne "none" -or (& $gitExe status --porcelain)) {
    & $gitExe add -A
    & $gitExe commit -m "release: $tag"
    if ($LASTEXITCODE -ne 0) { throw "commit failed" }
}

$existing = & $gitExe tag --list $tag
if ($existing) { throw "tag $tag already exists" }

& $gitExe tag -a $tag -m "REDKOlab $tag"
if ($LASTEXITCODE -ne 0) { throw "tagging failed" }

Invoke-GitPush $branch
Invoke-GitPush $tag

Write-Host ""
Write-Host "pushed $tag" -ForegroundColor Green
Write-Host "GitHub Actions will now: build -> self-check -> Blender 4.2/4.5/5.2 tests -> publish and archive to Releases"
Write-Host "watch it: gh run watch  (or the Actions tab on the repository page)"
