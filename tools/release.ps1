<#
.SYNOPSIS
    Verify, tag and push a REDKOlab release.

.DESCRIPTION
    Pushing the tag triggers .github/workflows/release.yml, which builds the
    package, self-checks its structure, runs the real Blender test suite on the
    3-combo matrix (4.0.2 auto / 5.2.2 auto / 5.2.2 extensions), then publishes
    and archives the zip in GitHub Releases.

    Steps performed here (before anything is pushed):
      1. git working tree must be clean
      2. optional version bump (bl_info + blender_manifest.toml, always in sync)
      3. version consistency check (tools/check_version.py)
      4. build the package (tools/build_zips.py)
      5. package self-check (tools/inspect_zip.py)
      6. real-Blender gate (tools/run_tests.ps1, default 4.0.2 / auto) - the
         release is aborted here if the add-on cannot be installed, enabled and
         pass its assertions, so a broken package can never reach a tag
      7. commit, tag and push. Only 2 push attempts per ref, each one under its own
         wall-clock limit, and an authentication failure or a timeout stops
         immediately (it is not retried). The transfer is always non-interactive:
         GCM_INTERACTIVE=never and GIT_TERMINAL_PROMPT=0 are set before pushing, so
         the credential helper fails instead of opening a "select an account"
         window (one such window parked a release push for 18 minutes). When the
         portable gh CLI is available it is used as a one-shot credential helper
         (-c credential.helper=..., never written to .git/config); otherwise a
         plain "git push" is used. The proxy is passed per invocation with -c only
         when -ProxyUrl is given - nothing is ever written to .git/config.

    NOTE: this file is intentionally ASCII-only. Windows PowerShell 5.1 reads
    .ps1 files using the system code page, which mangles non-ASCII text and can
    even break parsing.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\release.ps1
    powershell -ExecutionPolicy Bypass -File tools\release.ps1 -Bump patch
    powershell -ExecutionPolicy Bypass -File tools\release.ps1 -Bump minor -DryRun
    powershell -ExecutionPolicy Bypass -File tools\release.ps1 -SkipRealTest
    powershell -ExecutionPolicy Bypass -File tools\release.ps1 -ProxyUrl http://127.0.0.1:7890
#>
param(
    [ValidateSet("none", "patch", "minor", "major")][string]$Bump = "none",
    # Explicit opt-in required when the source version and the latest git tag
    # disagree (manual version edit). Without it the bump is refused, because
    # "current version + 1" on top of an already-bumped source skips a version.
    [switch]$AllowBumpOnTagMismatch,
    [string]$Remote = "origin",
    # Optional proxy for the push step ONLY. Git on Windows does not read the
    # system (WinINET) proxy, so on a machine where every browser works the push
    # can still fail; this passes the proxy explicitly as
    # "-c http.proxy=<url> -c https.proxy=<url>" on the push invocation.
    # Nothing is written to .git/config. Left empty (the default) the push
    # command is byte-for-byte the one used before this parameter existed.
    [string]$ProxyUrl = "",
    [string]$Git = "",
    # Non-interactive credential helper: the portable gh CLI. Empty (the default)
    # means "look for %LOCALAPPDATA%\redkolab-tools\gh\bin\gh.exe and use it if it
    # exists"; when nothing is found a plain "git push" is used.
    [string]$Gh = "",
    # Wall-clock limit for ONE push attempt, in seconds. An attempt that exceeds it
    # is killed and the release stops (see the .DESCRIPTION block).
    [ValidateRange(15, 600)][int]$PushTimeoutSeconds = 90,
    [string]$Python = "",
    # Local add-on zip library: every release is copied there, replacing older versions.
    # Real physical path; D:\build\Blender is a junction to C:\build\Blender (same folder).
    [string]$DeployTo = "C:\build\Blender\4.0\4.0chajian",
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

# --- GUARD2-BEGIN ---
function Find-ForcePush {
    # Guard 2: an operation that overwrites history is never allowed from this
    # script. If a network problem makes the transfer fail, it must stay failed.
    #
    # This is a self-scan: the guard reads this very file and looks at the
    # arguments of the actual push invocation. It cannot be bypassed by calling
    # a differently named helper later, because it does not care which function
    # issues the command.
    #
    # The forbidden marker is assembled at runtime on purpose: if it appeared
    # literally in this file, the guard would flag its own source code (that
    # really happened during development of this guard). The GUARD2 markers let
    # the scan skip this block for the same reason.
    $markerStart = "GUARD2" + "-BEGIN"
    $markerEnd = "GUARD2" + "-END"
    $dangerTokens = @(("--" + "force"), ("--" + "force" + "-with-lease"))

    $selfPath = $PSCommandPath
    if (-not $selfPath) { $selfPath = $MyInvocation.MyCommand.Path }
    $lines = @(Get-Content -LiteralPath $selfPath -ErrorAction SilentlyContinue)
    if ($lines.Count -eq 0) { return @() }

    $hits = @()
    $insideGuard = $false
    foreach ($line in $lines) {
        $text = $line.Trim()
        if ($text.Contains($markerStart)) { $insideGuard = $true; continue }
        if ($text.Contains($markerEnd)) { $insideGuard = $false; continue }
        if ($insideGuard) { continue }
        if ($text.Length -eq 0) { continue }
        if ($text[0] -eq "#") { continue }
        if ($text -notmatch '(^|\s)push(\s|$)') { continue }

        $parts = $text.Split(" ")
        for ($i = 0; $i -lt $parts.Count; $i++) {
            if ($parts[$i] -eq "push") {
                $arguments = @()
                if ($i + 1 -lt $parts.Count) { $arguments = $parts[($i + 1)..($parts.Count - 1)] }
                foreach ($token in $arguments) {
                    # Token-prefix match: "flag=value" style is caught too, while a
                    # branch name that merely contains the word is not a false hit.
                    foreach ($banned in $dangerTokens) {
                        if ($token -eq $banned) { $hits += $text }
                        elseif ($token.StartsWith($banned + "=")) { $hits += $text }
                    }
                    if ($token.StartsWith("+")) { $hits += $text }
                }
            }
        }
    }
    return $hits
}
# --- GUARD2-END ---

function Get-ProxyArgs {
    # Proxy settings, when requested, are applied to a single git invocation with
    # -c. Writing them into .git/config is deliberately NOT done: a release
    # script must not leave the repository permanently reconfigured, and a
    # one-off proxy should disappear together with the command that needed it.
    if (-not $ProxyUrl) { return @() }
    return @("-c", ("http.proxy=" + $ProxyUrl), "-c", ("https.proxy=" + $ProxyUrl))
}

# --- non-interactive push: no credential window, no unbounded wait -------------
# A release push must never be parked behind an interactive authorization window.
# On the 2.22.0 release the tag push sat in the GCM "select an account" window for
# 18 minutes and only completed after the tag was re-pushed by hand with
# non-interactive credentials. Three things prevent a repeat:
#   1. GCM_INTERACTIVE=never + GIT_TERMINAL_PROMPT=0: the credential helper fails
#      instead of opening a window, and git never waits for a typed username.
#   2. the portable gh CLI is preferred as a one-shot credential helper, so a
#      token is available without any prompt at all.
#   3. every attempt runs under a wall-clock limit and is killed when it expires.
$env:GCM_INTERACTIVE = "never"
$env:GIT_TERMINAL_PROMPT = "0"

$script:GhCredentialHelper = ""

function Resolve-GhCredentialHelper {
    # Picks the non-interactive credential source. gh keeps its own token, so
    # "gh auth git-credential" never prompts. Returns "" when gh is missing, and
    # also when its path contains whitespace: git runs a "!" helper through the
    # shell, so such a path cannot be passed safely from here - the normal git
    # credential setup is used instead (still non-interactive, see above).
    param([string]$Explicit)
    $candidates = @()
    if ($Explicit) { $candidates += $Explicit }
    $candidates += (Join-Path $portableRoot "gh\bin\gh.exe")
    foreach ($path in $candidates) {
        if (-not $path) { continue }
        if (-not (Test-Path -LiteralPath $path)) { continue }
        if ($path -match "\s") {
            Write-Host "note: gh was found but its path contains whitespace; the one-shot credential helper is skipped and the normal git credential setup is used" -ForegroundColor Yellow
            return ""
        }
        return $path
    }
    return ""
}

function Get-PushCredentialArgs {
    # One-shot flags for a single push. The empty helper value first RESETS the
    # helper list, so a configured interactive helper (GCM) is not consulted at
    # all; then gh is added as the only helper. Both are "-c" values: they apply
    # to this invocation only and are never written to .git/config.
    if (-not $script:GhCredentialHelper) { return @() }
    return @(
        "-c", "credential.helper=",
        "-c", ("credential.helper=!" + $script:GhCredentialHelper + " auth git-credential")
    )
}

function Format-PushCommand {
    # Renders exactly the argv that would be executed, for the -DryRun preview and
    # for error messages, so the printed command line is what really runs.
    param([string[]]$Arguments)
    $parts = @("git")
    foreach ($item in $Arguments) {
        if ($item -match "\s") {
            $parts += ('"' + $item + '"')
        } else {
            $parts += $item
        }
    }
    return ($parts -join " ")
}

function Assert-SafePushArguments {
    # The source scan (Find-ForcePush) only sees literal "git ... push ..." lines;
    # this checks the argument array that is really handed to git, so a flag that
    # overwrites history is refused no matter how the array was assembled. The
    # banned markers are assembled at runtime so this file does not carry them.
    param([string[]]$Arguments)
    $banned = @(("--" + "force"), ("--" + "force" + "-with-lease"), "-f")
    foreach ($item in $Arguments) {
        $token = ("$item").ToLowerInvariant()
        if ($token.StartsWith("+")) {
            throw "refusing to continue: a push argument starting with '+' would overwrite the remote side. Nothing here is allowed to rewrite history; a failed transfer must stay failed."
        }
        foreach ($marker in $banned) {
            if ($token -eq $marker -or $token.StartsWith($marker + "=")) {
                throw ("refusing to continue: a history-overwriting argument was rejected before running git: {0}. Such arguments are never allowed here." -f $item)
            }
        }
    }
}

function Invoke-PushAttempt {
    # Runs exactly one push under a wall-clock limit. A real System.Diagnostics
    # process is used (not a plain "&") because only it can be killed: a stuck TLS
    # handshake, or a helper that ignores the never-interactive setting, would
    # otherwise block the release forever. Start-Process -PassThru was tried first
    # and rejected: on this PowerShell 5.1 build it returns an EMPTY ExitCode even
    # after a blocking WaitForExit, which would hide every failure behind an
    # "exit=" with no number (exit codes must always be checked).
    param([string[]]$Arguments, [int]$TimeoutSeconds)

    $quoted = @()
    foreach ($item in $Arguments) {
        if ($item -match "\s") {
            $quoted += ('"' + $item + '"')
        } else {
            $quoted += $item
        }
    }

    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $gitExe
    $info.Arguments = ($quoted -join " ")
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    # An empty stdin pipe plus GIT_TERMINAL_PROMPT=0 means git cannot even try to
    # ask for a username.
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $info.StandardErrorEncoding = [System.Text.Encoding]::UTF8

    $result = @{ ExitCode = -1; TimedOut = $false; Lines = @(); Seconds = 0 }
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    $started = Get-Date
    try {
        $process.Start() | Out-Null
        # Read asynchronously: a full pipe buffer would deadlock the child.
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $finished = $process.WaitForExit($TimeoutSeconds * 1000)
        $result.Seconds = [int]((Get-Date) - $started).TotalSeconds
        if (-not $finished) {
            $result.TimedOut = $true
            try { $process.Kill() } catch { }
            try { $process.WaitForExit(5000) | Out-Null } catch { }
            $result.Lines = @(
                ("killed after {0}s: no response (the attempt was waiting on a credential window or a stuck connection)" -f $TimeoutSeconds)
            )
            return $result
        }
        $result.ExitCode = $process.ExitCode
        $lines = @()
        if ($stdoutTask.Wait(5000)) { $lines += @($stdoutTask.Result -split "`r?`n") }
        if ($stderrTask.Wait(5000)) { $lines += @($stderrTask.Result -split "`r?`n") }
        $result.Lines = @($lines | Where-Object { $_ -and $_.Trim() })
        return $result
    } finally {
        if ($process) { $process.Dispose() }
    }
}

function Get-SystemProxyHint {
    # Windows keeps its proxy in the registry and git never consults it, so a
    # transfer can fail on a machine where every browser works. Read the value
    # and only *report* it - it is never applied automatically: silently routing
    # a release transfer through an unexpected proxy is worse than a slow one.
    try {
        $key = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        $enabled = (Get-ItemProperty -Path $key -Name ProxyEnable -ErrorAction Stop).ProxyEnable
        if (-not $enabled) { return "" }
        $server = (Get-ItemProperty -Path $key -Name ProxyServer -ErrorAction Stop).ProxyServer
        if (-not $server) { return "" }
        $candidate = ""
        if ($server -match "=") {
            # Per-protocol form: "http=host:port;https=host:port;ftp=..."
            foreach ($piece in $server.Split(";")) {
                $eq = $piece.IndexOf("=")
                if ($eq -gt 0 -and $piece.Substring(0, $eq).Trim().ToLowerInvariant() -eq "http") {
                    $candidate = $piece.Substring($eq + 1).Trim()
                }
            }
        } else {
            # Single form: "host:port" applies to every protocol.
            $candidate = $server.Trim()
        }
        if (-not $candidate) { return "" }
        if ($candidate -notmatch "^(https?|socks[45]?)://") { $candidate = "http://" + $candidate }
        # Only report something git can actually parse: scheme://host:port.
        if ($candidate -notmatch "^[a-zA-Z][a-zA-Z0-9+.\-]*://[^/\s]+:[0-9]+$") { return "" }
        return $candidate
    } catch {
        return ""
    }
}

# Authentication failures are NOT retried. When the credential helper (GCM) has a
# stale entry, every push attempt pops a GitHub "select an account" window; blind
# retrying turns a single failure into a popup storm. Match the usual messages and
# stop after the first one.
$AuthFailureMarkers = @(
    "Authentication failed",
    "could not read Username",
    "Invalid username or password",
    "terminal prompts disabled",
    "fatal: could not read"
)

function Test-AuthFailure {
    param([string]$Text)
    if (-not $Text) { return $false }
    foreach ($marker in $AuthFailureMarkers) {
        if ($Text.IndexOf($marker, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }
    }
    return $false
}

function Invoke-GitPush {
    # Attempts: 2 (not 6), each with its own wall-clock limit. Every attempt could
    # still reach a credential helper, so the retry budget stays tiny. An
    # authentication failure or a timeout stops immediately (it is not retried):
    # retrying those only repeats the same wait.
    param([string]$Reference, [int]$Attempts = 2)

    $forbidden = @(Find-ForcePush)
    if ($forbidden.Count -gt 0) {
        throw ("refusing to continue: history-overwriting arguments were found in this script: " + ($forbidden -join " | ") + " - such arguments are never allowed here; a failed transfer must stay failed.")
    }
    if ($Reference.StartsWith("+")) {
        throw "refusing to continue: the refspec argument starts with '+', which overwrites the remote side. Use a normal refspec and let a failure stay a failure."
    }

    $arguments = @()
    $arguments += @(Get-PushCredentialArgs)
    $arguments += @(Get-ProxyArgs)
    $arguments += @("push")
    $arguments += @($Remote)
    $arguments += @($Reference)
    Assert-SafePushArguments -Arguments $arguments

    Write-Host ("push command: " + (Format-PushCommand -Arguments $arguments)) -ForegroundColor Cyan
    Write-Host ("non-interactive: GCM_INTERACTIVE=never, GIT_TERMINAL_PROMPT=0, per-attempt timeout {0}s, at most {1} attempt(s)" -f $PushTimeoutSeconds, $Attempts) -ForegroundColor Cyan

    for ($i = 1; $i -le $Attempts; $i++) {
        $result = Invoke-PushAttempt -Arguments $arguments -TimeoutSeconds $PushTimeoutSeconds
        foreach ($line in $result.Lines) { Write-Host ("  push: " + $line) }
        if (-not $result.TimedOut -and $result.ExitCode -eq 0) { return }
        $first = ""
        foreach ($line in $result.Lines) {
            if ($line -and $line.Trim()) { $first = $line.Trim(); break }
        }
        if ($result.TimedOut) {
            throw ("git push {0} timed out after {1}s and was killed (attempt {2}/{3}). Stopping here: a hung attempt is waiting for something it will not get, and retrying only repeats the wait. How to fix: (1) push through the local proxy: powershell -ExecutionPolicy Bypass -File tools\release.ps1 -ProxyUrl http://127.0.0.1:7890 (add -Bump none if the version is already bumped); (2) check the non-interactive credential source works: gh auth status; (3) check the connection to github.com. Nothing was forced and the remote was left as it was." -f $Reference, $PushTimeoutSeconds, $i, $Attempts)
        }
        Write-Host ("push {0} failed (attempt {1}/{2}, exit={3}, {4}s): {5}" -f $Reference, $i, $Attempts, $result.ExitCode, $result.Seconds, $first) -ForegroundColor Yellow
        if (Test-AuthFailure ($result.Lines -join "`n")) {
            throw ("GitHub authentication failed (exit={0}) on 'git push {1}'. Not retrying: every attempt could pop another authorization window, and this run is already non-interactive (GCM_INTERACTIVE=never). How to fix: (1) if a window asks you to select an account, pick the right one; (2) push through the local proxy: powershell -ExecutionPolicy Bypass -File tools\release.ps1 -ProxyUrl http://127.0.0.1:7890 (add -Bump none if the version is already bumped); (3) check the non-interactive credential source: gh auth status; (4) stale cached credential: cmdkey /delete:git:https://github.com then retry. Nothing was forced and the remote was left as it was." -f $result.ExitCode, $Reference)
        }
        if ($i -lt $Attempts) {
            Write-Host ("retrying in 3s (at most {0} attempts total; every attempt runs under a {1}s timeout)" -f $Attempts, $PushTimeoutSeconds) -ForegroundColor Yellow
            Start-Sleep -Seconds 3
        }
    }
    throw ("git push {0} failed after {1} attempts (each with a {2}s timeout). Nothing was forced and the remote was left as it was. If it is a network problem, retry with -ProxyUrl http://127.0.0.1:7890" -f $Reference, $Attempts, $PushTimeoutSeconds)
}
# --- PUSH-POLICY-END ---
# Everything above this marker is definitions only. The marker lets a throwaway
# harness dot-source exactly this push policy (helpers + Invoke-GitPush) and drive
# it against a fake git executable, so the timeout / non-interactive / one-shot
# credential arguments can be exercised without publishing a release.

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
    # Guard 1: refuse to bump when the source version and the newest tag disagree.
    # How it went wrong once: the version was edited by hand and then released
    # with "-Bump minor", so "current source version + 1" skipped a whole
    # version, and the wrong tag/commit reached main. Confirm explicitly with
    # -AllowBumpOnTagMismatch when the mismatch is intentional.
    $latestTag = (& $gitExe describe --tags --abbrev=0 2>$null | Out-String).Trim()
    if (-not $latestTag) {
        throw "cannot determine the latest git tag (git describe --tags --abbrev=0 returned nothing) - refusing to bump because the 'current version + 1' result cannot be cross-checked against a tag"
    }
    $tagVersion = $latestTag
    if ($tagVersion.StartsWith("v") -or $tagVersion.StartsWith("V")) {
        $tagVersion = $tagVersion.Substring(1)
    }
    $sourceVersion = ($version -join ".")
    if ($tagVersion -ne $sourceVersion -and -not $AllowBumpOnTagMismatch) {
        throw ("source version {0} does not match the latest git tag {1} - the version was probably already edited by hand, so bumping again would skip a version. Re-run with -Bump none for an already-bumped source, or pass -AllowBumpOnTagMismatch if the mismatch is intentional." -f $sourceVersion, $latestTag)
    }

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

# Non-interactive credential source for the transfer (see the helpers above).
$script:GhCredentialHelper = Resolve-GhCredentialHelper -Explicit $Gh
if ($script:GhCredentialHelper) {
    Write-Host ("credential helper for the transfer: " + $script:GhCredentialHelper + " (one-shot -c, non-interactive)") -ForegroundColor Cyan
} else {
    Write-Host "no portable gh CLI found: using the normal git credential setup (GCM_INTERACTIVE=never still prevents an interactive window)" -ForegroundColor Yellow
}

# The proxy is used for the transfer only. Say which one (if any) up front so a
# -DryRun preview shows exactly what the real run would use.
if ($ProxyUrl) {
    Write-Host "proxy for the transfer: $ProxyUrl" -ForegroundColor Cyan
} else {
    $proxyHint = Get-SystemProxyHint
    if ($proxyHint) {
        Write-Host ("hint: the Windows system proxy is {0} and git does not read it; if the upload times out, re-run with -ProxyUrl {0}" -f $proxyHint) -ForegroundColor Yellow
    }
}

if ($DryRun) {
    $previewArgs = @()
    $previewArgs += @(Get-PushCredentialArgs)
    $previewArgs += @(Get-ProxyArgs)
    $previewArgs += @("push")
    $branchArgs = @($previewArgs) + @($Remote, $branch)
    $tagArgs = @($previewArgs) + @($Remote, $tag)
    Write-Host ""
    Write-Host "DryRun: all checks passed, nothing was committed or pushed." -ForegroundColor Yellow
    Write-Host "Would run:"
    Write-Host "  git add -A"
    Write-Host "  git commit -m `"release: $tag`""
    Write-Host "  git tag -a $tag"
    Write-Host ("  " + (Format-PushCommand -Arguments $branchArgs))
    Write-Host ("  " + (Format-PushCommand -Arguments $tagArgs))
    Write-Host ("  (each push: GCM_INTERACTIVE=never, GIT_TERMINAL_PROMPT=0, timeout {0}s, at most 2 attempts; -c values are per invocation, nothing is written to .git/config)" -f $PushTimeoutSeconds)
    exit 0
}

if ($Bump -ne "none" -or (& $gitExe status --porcelain)) {
    & $gitExe add -A
    & $gitExe commit -m "release: $tag"
    if ($LASTEXITCODE -ne 0) { throw "commit failed" }
}

# An already released tag is never moved: a local tag with this name aborts the
# release here, and even if a copy exists only on the remote, "git push <tag>" is
# rejected by the remote without rewriting it (this script never passes a flag
# that could change that).
$existing = & $gitExe tag --list $tag
if ($existing) { throw "tag $tag already exists" }

& $gitExe tag -a $tag -m "REDKOlab $tag"
if ($LASTEXITCODE -ne 0) { throw "tagging failed" }

Invoke-GitPush $branch
Invoke-GitPush $tag

Write-Host ""
Write-Host "pushed $tag" -ForegroundColor Green
Write-Host "GitHub Actions will now: build -> self-check -> Blender 4.0.2 auto / 5.2.2 auto / 5.2.2 extensions tests -> publish and archive to Releases"
Write-Host "watch it: gh run watch  (or the Actions tab on the repository page)"
