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
      6. commit, tag and push

    ASCII-only on purpose: Windows PowerShell 5.1 reads .ps1 files using the
    system code page and would mangle non-ASCII text without a BOM.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\release.ps1
    powershell -ExecutionPolicy Bypass -File tools\release.ps1 -Bump patch
    powershell -ExecutionPolicy Bypass -File tools\release.ps1 -Bump minor -DryRun
#>
param(
    [ValidateSet("none", "patch", "minor", "major")][string]$Bump = "none",
    [string]$Remote = "origin",
    [string]$Git = "",
    [string]$Python = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

# --- locate tools -----------------------------------------------------------
function Resolve-Tool([string]$explicit, [string]$name, [string]$portableFile) {
    if ($explicit) { return $explicit }
    $command = Get-Command $name -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    if ($portableFile -and (Test-Path -LiteralPath $portableFile)) {
        $path = (Get-Content -LiteralPath $portableFile -Raw).Trim()
        if ($path -and (Test-Path -LiteralPath $path)) { return $path }
    }
    return $null
}

$portableRoot = Join-Path $env:LOCALAPPDATA "redkolab-tools"
$gitExe = Resolve-Tool $Git "git" (Join-Path $portableRoot "git_path.txt")
if (-not $gitExe) { throw "找不到 git（可用 -Git 指定路径）" }

if (-not $Python) {
    $Python = Resolve-Tool "" "python"
    if (-not $Python) { $Python = Resolve-Tool "" "py" }
}
if (-not $Python) { throw "找不到 python（可用 -Python 指定路径）" }

$packageDir = Join-Path $projectRoot "redkolab"
$initPath = Join-Path $packageDir "__init__.py"
$manifestPath = Join-Path $packageDir "blender_manifest.toml"

function Get-CurrentVersion {
    $text = Get-Content -LiteralPath $initPath -Raw -Encoding UTF8
    $match = [regex]::Match($text, '"version"\s*:\s*\((\d+)\s*,\s*(\d+)\s*,\s*(\d+)\)')
    if (-not $match.Success) { throw "在 __init__.py 里找不到 bl_info 的 version" }
    return @([int]$match.Groups[1].Value, [int]$match.Groups[2].Value, [int]$match.Groups[3].Value)
}

function Set-Version([int[]]$parts) {
    $text = Get-Content -LiteralPath $initPath -Raw -Encoding UTF8
    $text = [regex]::Replace($text, '("version"\s*:\s*\()\d+\s*,\s*\d+\s*,\s*\d+(\))',
        ('${1}' + $parts[0] + ', ' + $parts[1] + ', ' + $parts[2] + '${2}'))
    Set-Content -LiteralPath $initPath -Value $text -Encoding UTF8 -NoNewline

    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8
    $manifest = [regex]::Replace($manifest, '(?m)^version\s*=\s*"[^"]*"',
        ('version = "' + ($parts -join '.') + '"'))
    Set-Content -LiteralPath $manifestPath -Value $manifest -Encoding UTF8 -NoNewline
}

# --- 1. clean tree ----------------------------------------------------------
$status = & $gitExe status --porcelain
if ($status) {
    Write-Host "工作区不干净，先提交或还原：" -ForegroundColor Yellow
    & $gitExe status --short
    throw "git working tree is not clean"
}

# --- 2. bump ----------------------------------------------------------------
$version = Get-CurrentVersion
if ($Bump -ne "none") {
    switch ($Bump) {
        "major" { $version[0]++; $version[1] = 0; $version[2] = 0 }
        "minor" { $version[1]++; $version[2] = 0 }
        "patch" { $version[2]++ }
    }
    Set-Version $version
    Write-Host ("版本号已升到 " + ($version -join ".")) -ForegroundColor Cyan
}
$versionText = ($version -join ".")
$tag = "v$versionText"

# --- 3. checks --------------------------------------------------------------
& $Python (Join-Path $PSScriptRoot "check_version.py") $tag
if ($LASTEXITCODE -ne 0) { throw "版本一致性检查失败" }

& $Python (Join-Path $PSScriptRoot "build_zips.py")
if ($LASTEXITCODE -ne 0) { throw "打包失败" }

$zips = Get-ChildItem -LiteralPath (Join-Path $projectRoot "dist") -Filter "*.zip"
& $Python (Join-Path $PSScriptRoot "inspect_zip.py") $zips.FullName
if ($LASTEXITCODE -ne 0) { throw "安装包自检失败，已中止发布" }

# --- 4. commit / tag / push -------------------------------------------------
$branch = (& $gitExe rev-parse --abbrev-ref HEAD).Trim()

if ($DryRun) {
    Write-Host ""
    Write-Host "DryRun：以上全部通过，未提交也未推送。将要执行：" -ForegroundColor Yellow
    Write-Host "  git add -A"
    Write-Host "  git commit -m `"release: $tag`""
    Write-Host "  git tag -a $tag -m `"$tag`""
    Write-Host "  git push $Remote $branch"
    Write-Host "  git push $Remote $tag"
    exit 0
}

if ($Bump -ne "none" -or (& $gitExe status --porcelain)) {
    & $gitExe add -A
    & $gitExe commit -m "release: $tag"
    if ($LASTEXITCODE -ne 0) { throw "提交失败" }
}

$existing = & $gitExe tag --list $tag
if ($existing) { throw "tag $tag 已存在，先删除或换个版本号" }

& $gitExe tag -a $tag -m "红红子lab (REDKOlab) $tag"
if ($LASTEXITCODE -ne 0) { throw "打 tag 失败" }

& $gitExe push $Remote $branch
if ($LASTEXITCODE -ne 0) { throw "推送分支失败" }
& $gitExe push $Remote $tag
if ($LASTEXITCODE -ne 0) { throw "推送 tag 失败" }

Write-Host ""
Write-Host "已推送 $tag" -ForegroundColor Green
Write-Host "GitHub Actions 会自动：打包 -> 结构自检 -> Blender 4.2/4.5/5.2 真机测试 -> 发布并归档到 Releases"
Write-Host "进度：仓库页面的 Actions 标签页（发布完成后 Releases 里会有 $tag 的归档）"
