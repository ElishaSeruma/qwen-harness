<#
.SYNOPSIS
    Build and launch the native Windows desktop app.

.DESCRIPTION
    The Windows counterpart of script/build_and_run.sh. Builds
    windows/QwenHarnessDesktop with the .NET SDK, stamps the repo root next to
    the executable (the equivalent of the Mac bundle's repo-root.txt resource),
    renders the app icon, and launches it.

    The browser flow (wake-up.ps1) still works unchanged and shares the same
    backend, the same port, and the same ~/.qwen-harness state directory —
    the desktop window is a second client, not a second harness.

.PARAMETER Mode
    run       build and launch (default)
    debug     build a Debug configuration and launch it
    logs      launch, then tail the server log
    verify    launch, confirm the process is actually alive, and report
    build     build only, do not launch

.EXAMPLE
    .\script\build_and_run.ps1
    .\script\build_and_run.ps1 -Mode verify
#>
[CmdletBinding()]
param(
    [ValidateSet('run', 'debug', 'logs', 'verify', 'build')]
    [string]$Mode = 'run'
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$ProjectDir = Join-Path $Root 'windows\QwenHarnessDesktop'
$Project = Join-Path $ProjectDir 'QwenHarnessDesktop.csproj'
$Configuration = if ($Mode -eq 'debug') { 'Debug' } else { 'Release' }
$OutDir = Join-Path $ProjectDir "bin\$Configuration\net8.0-windows"
$Exe = Join-Path $OutDir 'QwenHarnessDesktop.exe'
$LogoPng = Join-Path $Root 'AppLogo.png'
$IconPath = Join-Path $ProjectDir 'AppIcon.ico'

function Write-Step($text) { Write-Host "`n── $text " -ForegroundColor Cyan -NoNewline; Write-Host ('─' * [Math]::Max(0, 55 - $text.Length)) -ForegroundColor DarkCyan }

# ── 0. prerequisites ────────────────────────────────────────────────────
Write-Step '0. Prerequisites'

if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    throw "The .NET SDK is required. Install it from https://dotnet.microsoft.com/download"
}
Write-Host "dotnet SDK $(dotnet --version)"

# WebView2 is a *runtime* dependency, not a bundled one. Checking here turns a
# confusing "class not registered" at first paint into one actionable line.
$wv2Keys = @(
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
    'HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
    'HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
)
$wv2 = $wv2Keys | ForEach-Object { if (Test-Path $_) { (Get-ItemProperty $_).pv } } | Where-Object { $_ } | Select-Object -First 1
if ($wv2) {
    Write-Host "WebView2 Runtime $wv2"
} else {
    Write-Warning "WebView2 Runtime not found. Install it from https://developer.microsoft.com/microsoft-edge/webview2/ before launching."
}

# ── 1. stop anything we are about to replace ────────────────────────────
Write-Step '1. Stopping a previous instance'
# Stop-Process is asynchronous: it signals, it does not block. A running
# instance keeps an exclusive handle on its own .exe, so building immediately
# afterwards raced the shutdown and failed with MSB3027 "Exceeded retry count
# of 10 ... locked by Qwen Harness". Wait for the process to actually be gone,
# the same way the Ollama restart waits for the port to free rather than
# sleeping and hoping.
$previous = @(Get-Process -Name 'QwenHarnessDesktop' -ErrorAction SilentlyContinue)
if ($previous.Count -gt 0) {
    foreach ($proc in $previous) {
        Write-Host "Stopping PID $($proc.Id)"
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    }
    for ($i = 0; $i -lt 40; $i++) {
        if (-not (Get-Process -Name 'QwenHarnessDesktop' -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 250
    }
    if (Get-Process -Name 'QwenHarnessDesktop' -ErrorAction SilentlyContinue) {
        throw 'A previous Qwen Harness instance is still running and holds its executable. Close it and retry.'
    }
    Write-Host 'Previous instance exited.'
}
# Unlike the Mac script, the GUI server is deliberately left running: it is
# shared with any browser tab you have open, and the app reuses a healthy one
# rather than starting a second.

# ── 2. app icon ─────────────────────────────────────────────────────────
Write-Step '2. App icon'
if ((Test-Path $LogoPng) -and (Get-Command python -ErrorAction SilentlyContinue)) {
    try {
        python (Join-Path $Root 'script\render_app_icon_windows.py') $LogoPng $IconPath
    } catch {
        Write-Warning "Icon render skipped: $($_.Exception.Message)"
    }
} else {
    Write-Host 'Skipped (no AppLogo.png or no python).'
}
if (-not (Test-Path $IconPath)) {
    # The csproj declares <ApplicationIcon>, and msbuild fails hard if the file
    # is missing. A 1x1 placeholder keeps the build honest without pretending
    # to be the real artwork.
    Write-Warning 'No AppIcon.ico — building without a custom icon.'
}

# ── 3. build ────────────────────────────────────────────────────────────
Write-Step "3. Building ($Configuration)"
dotnet build $Project -c $Configuration --nologo -v minimal
if ($LASTEXITCODE -ne 0) { throw "dotnet build failed ($LASTEXITCODE)" }

# ── 4. stamp the repo root ──────────────────────────────────────────────
# The Mac build writes this into Contents/Resources; here it sits beside the
# exe. Without it, an app launched from a shortcut has no idea where the
# checkout containing gui/server.py lives.
Write-Step '4. Stamping the repo root'
Set-Content -Path (Join-Path $OutDir 'repo-root.txt') -Value $Root -Encoding utf8 -NoNewline
Write-Host "repo-root.txt -> $Root"

if ($Mode -eq 'build') {
    Write-Host "`nBuilt: $Exe" -ForegroundColor Green
    return
}

# ── 5. launch ───────────────────────────────────────────────────────────
Write-Step '5. Launching'
if (-not (Test-Path $Exe)) { throw "Build produced no executable at $Exe" }
$process = Start-Process -FilePath $Exe -PassThru
Write-Host "Launched $Exe (PID $($process.Id))" -ForegroundColor Green

switch ($Mode) {
    'verify' {
        Start-Sleep -Seconds 3
        $alive = Get-Process -Id $process.Id -ErrorAction SilentlyContinue
        if (-not $alive) { throw 'The app exited immediately — check the WebView2 Runtime and Python installs.' }
        Write-Host "`nVerified: Qwen Harness is running (PID $($process.Id))." -ForegroundColor Green
        # Confirm the backend it started is actually answering, not just that a
        # window exists. "It launched" and "it works" are different claims.
        Start-Sleep -Seconds 5
        try {
            $health = Invoke-RestMethod 'http://127.0.0.1:8420/api/health' -TimeoutSec 5
            Write-Host "Backend health: ok=$($health.ok)" -ForegroundColor Green
        } catch {
            Write-Warning "Backend not answering on :8420 yet — the app scans 8420-8439, check its status bar."
        }
    }
    'logs' {
        $log = Join-Path $env:TEMP 'qwen-harness-gui.log'
        Write-Host "Tailing $log (Ctrl+C to stop)`n"
        for ($i = 0; $i -lt 20 -and -not (Test-Path $log); $i++) { Start-Sleep -Milliseconds 500 }
        if (Test-Path $log) { Get-Content $log -Wait -Tail 40 } else { Write-Warning "No log at $log yet." }
    }
}
