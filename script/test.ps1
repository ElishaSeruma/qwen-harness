<#
.SYNOPSIS
    Run the full check suite on Windows.

.DESCRIPTION
    The Windows counterpart of script/test.sh: the same JavaScript syntax
    checks, the same Python compile pass, the same unittest discovery, plus a
    build of the native desktop app.

    The .NET build is included deliberately. On the Mac, `swift build` is part
    of build_and_run.sh and a broken app surfaces immediately; here the app is
    a separate project that a Python-only change can silently break (the
    desktop bridge protocol lives on both sides of the line).
#>
[CmdletBinding()]
param(
    [switch]$SkipDesktopBuild
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$failures = @()
function Check($name, [scriptblock]$body) {
    Write-Host "  $name ... " -NoNewline
    # Two traps, both of which made this script report red on a green suite:
    #
    #  1. $ErrorActionPreference = 'Stop' turns ANY output a native command
    #     writes to stderr into a terminating error. `python -m unittest`
    #     writes its progress dots and its final "OK" to stderr, so a fully
    #     passing suite was reported as a failure. Only the exit code is a
    #     verdict here.
    #  2. $LASTEXITCODE is global and sticky. A check that runs no native
    #     command at all (the PowerShell parse checks) inherited whatever the
    #     previous one left behind, and failed with a code it never set.
    $global:LASTEXITCODE = 0
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $body 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "exit $LASTEXITCODE`n$($output | Select-Object -Last 12 | Out-String)"
        }
        Write-Host 'ok' -ForegroundColor Green
    } catch {
        Write-Host 'FAILED' -ForegroundColor Red
        Write-Host "    $($_.Exception.Message)" -ForegroundColor DarkRed
        $script:failures += $name
    } finally {
        $ErrorActionPreference = $previous
    }
}

Write-Host "`n── JavaScript syntax ───────────────────────────────" -ForegroundColor Cyan
$jsFiles = @(
    'gui/app.js', 'gui/agent-loop.js', 'gui/capabilities.js', 'gui/chats.js',
    'gui/effort.js', 'gui/runs.js', 'gui/filetree.js', 'gui/build.cjs',
    # Generated avatar modules had no syntax coverage at all - a bad splice in
    # build.cjs would have shipped silently.
    'gui/engine/avatar-shared.js', 'gui/engine/avatars/strobi.avatar.js'
)
foreach ($f in $jsFiles) { Check $f { node --check $f } }

$avatarProbe = @'
import('./gui/engine/avatar-shared.js').then(m => { if (typeof m.createAvatarFactory !== 'function') { console.error('missing createAvatarFactory'); process.exit(1) } })
'@
Check 'avatar-shared exports createAvatarFactory' { node --input-type=module -e $avatarProbe }

$effortProbe = @'
import { inferEffort, parseEffortEscalation } from './gui/effort.js';
if (inferEffort('hello') !== 1) process.exit(1);
if (inferEffort('Implement a production database migration, update files, add tests, then audit security and deploy. '.repeat(18)) !== 4) process.exit(1);
const request = parseEffortEscalation('[[REQUEST_MAX_EFFORT: verify risky migration]]');
if (request?.level !== 4 || request.reason !== 'verify risky migration') process.exit(1);
'@
Check 'effort inference' { node --input-type=module -e $effortProbe }

Write-Host "`n── Python ──────────────────────────────────────────" -ForegroundColor Cyan
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $python) { throw 'Python not found.' }

Check 'py_compile' {
    $files = @('gui/server.py', 'agent.py', 'orchestrator.py', 'script/render_app_icon.py',
               'script/render_app_icon_windows.py') + (Get-ChildItem tools/*.py | ForEach-Object { $_.FullName })
    & $python -m py_compile @files
}
Check 'unittest' { & $python -m unittest discover -s tests -q }

Write-Host "`n── PowerShell syntax ───────────────────────────────" -ForegroundColor Cyan
foreach ($ps1 in @('setup.ps1', 'wake-up.ps1', 'script/build_and_run.ps1', 'script/test.ps1')) {
    Check $ps1 {
        # Parse without executing. A .ps1 saved without a UTF-8 BOM is read as
        # ANSI by Windows PowerShell 5.1, which mangles every non-ASCII
        # character mid-string and breaks the parse - this catches that.
        $errors = $null
        [System.Management.Automation.Language.Parser]::ParseFile(
            (Resolve-Path $ps1).Path, [ref]$null, [ref]$errors) | Out-Null
        if ($errors -and $errors.Count -gt 0) { throw ($errors[0].Message) }
    }
}

if (-not $SkipDesktopBuild) {
    Write-Host "`n── Native desktop app ──────────────────────────────" -ForegroundColor Cyan
    if (Get-Command dotnet -ErrorAction SilentlyContinue) {
        Check 'dotnet build' {
            dotnet build windows/QwenHarnessDesktop/QwenHarnessDesktop.csproj -c Release --nologo -v quiet
        }
    } else {
        Write-Host '  skipped (no .NET SDK)' -ForegroundColor DarkGray
    }
}

Write-Host ''
if ($failures.Count -gt 0) {
    Write-Host "FAILED: $($failures -join ', ')" -ForegroundColor Red
    exit 1
}
Write-Host 'All checks passed.' -ForegroundColor Green
