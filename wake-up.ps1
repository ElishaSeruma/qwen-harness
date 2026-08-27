<#
.SYNOPSIS
    Qwen Harness — wake-up: launch everything for a working session.

.DESCRIPTION
    The Windows counterpart of wake-up.sh. Starts Ollama with the harness's
    runtime tuning, starts the Control Room backend, and opens it in your
    browser. (setup.ps1 = one-time install. wake-up.ps1 = every-day launch.)

.PARAMETER Port
    Port for the Control Room backend. Default 8420.

.PARAMETER NoBrowser
    Start everything but do not open a browser window.

.EXAMPLE
    .\wake-up.ps1
    .\wake-up.ps1 -Port 9000
#>
[CmdletBinding()]
param(
    [int]$Port = 8420,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSCommandPath)

function Write-Step($n, $text) {
    Write-Host "`n── $n. $text " -ForegroundColor Cyan -NoNewline
    Write-Host ('─' * [Math]::Max(0, 52 - $text.Length)) -ForegroundColor DarkCyan
}

function Test-Listening($p) {
    try { return [bool](Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction Stop) }
    catch { return $false }
}

# ── 1. Ollama ───────────────────────────────────────────────────────────
Write-Step 1 'Ollama'

# Runtime tuning. These are server-level environment variables: the
# OpenAI-compatible /v1 endpoint cannot set num_ctx or keep_alive per request,
# so they have to be set on the process. They only take effect on a fresh
# `ollama serve`, which is why this restarts it rather than skipping when it
# is already running.
#
# OLLAMA_CONTEXT_LENGTH: Ollama defaults to 4096 regardless of what a model
#   advertises, and 4096 cannot hold the tool schemas plus a source file plus
#   the conversation. On overflow Ollama does not error — it silently drops the
#   oldest content, which reads as a hallucination rather than as truncation.
#   16384 rather than the Mac's 8192 because this machine class has 16 GB;
#   tools/context_budget.py sizes the per-request value from real RAM, and this
#   is the floor for anything that does not go through the agent loop.
# OLLAMA_KEEP_ALIVE: avoids paying the multi-second model reload per message.
# OLLAMA_MAX_LOADED_MODELS=1: two resident contexts still thrash, even at 16 GB.
# OLLAMA_NUM_PARALLEL=1: gui/server.py's resource monitor pairs loaded models
#   to runner processes by list order, which assumes this.
if (-not $env:OLLAMA_CONTEXT_LENGTH)   { $env:OLLAMA_CONTEXT_LENGTH = '16384' }
if (-not $env:OLLAMA_KEEP_ALIVE)       { $env:OLLAMA_KEEP_ALIVE = '30m' }
if (-not $env:OLLAMA_MAX_LOADED_MODELS){ $env:OLLAMA_MAX_LOADED_MODELS = '1' }
if (-not $env:OLLAMA_NUM_PARALLEL)     { $env:OLLAMA_NUM_PARALLEL = '1' }

# Ollama binds 127.0.0.1 only, and `localhost` resolves to ::1 FIRST on
# Windows 11. Every probe below therefore uses the literal IPv4 address:
# measured, a `localhost` probe with -TimeoutSec 1 fails in 1083 ms (the ::1
# attempt eats the whole budget before the IPv4 fallback), while the same
# probe against 127.0.0.1 answers in 146 ms. The symptom was wake-up.ps1
# reporting "Ollama did not come up" every single run about a server that was
# already listening and serving.
$ollama = @(
    (Get-Command ollama -ErrorAction SilentlyContinue).Source,
    (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'),
    (Join-Path $env:ProgramFiles 'Ollama\ollama.exe')
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

if (-not $ollama) {
    Write-Warning "Ollama isn't installed. Run .\setup.ps1 first."
} else {
    $running = @(Get-Process -Name 'ollama' -ErrorAction SilentlyContinue)
    if ($running.Count -gt 0) {
        # Warn loudly if a model is loaded — restarting aborts any in-flight
        # run in another tab or in the desktop app.
        try {
            $ps = Invoke-RestMethod 'http://127.0.0.1:11434/api/ps' -TimeoutSec 3
            if ($ps.models.Count -gt 0) {
                Write-Warning 'A model is currently loaded. Restarting Ollama will abort any run in progress.'
            }
        } catch { }
        Write-Host "Restarting Ollama to apply context=$($env:OLLAMA_CONTEXT_LENGTH) keep_alive=$($env:OLLAMA_KEEP_ALIVE)"
        $running | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
        # Wait for the port to actually free rather than sleeping and hoping:
        # a fresh serve that loses the bind race exits without a message.
        for ($i = 0; $i -lt 20 -and (Test-Listening 11434); $i++) { Start-Sleep -Milliseconds 500 }
    }

    Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden

    # The Ollama desktop tray app respawns `ollama serve` on its own, without
    # this environment, and can win the bind race — at which point our process
    # exits quietly and the settings are silently reverted. Two consequences,
    # both handled here rather than left to surprise you:
    #
    #  1. The wait has to be longer when the tray app is present. A cold start
    #     answers in ~2s; a tray-app respawn after a kill took longer than the
    #     old 15s ceiling, so this reported "did not come up" about a server
    #     that was seconds from listening.
    #  2. Whichever process won, say so, because "Ollama is up" and "Ollama is
    #     up with the context length this harness needs" are different claims.
    $tray = [bool](Get-Process -Name 'ollama app' -ErrorAction SilentlyContinue)
    $waitSeconds = if ($tray) { 45 } else { 15 }
    $up = $false
    for ($i = 0; $i -lt ($waitSeconds * 2); $i++) {
        Start-Sleep -Milliseconds 500
        try { Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3 | Out-Null; $up = $true; break } catch { }
    }

    if ($up) { Write-Host 'Ollama started' } else { Write-Warning "Ollama did not come up within ${waitSeconds}s" }

    if ($tray) {
        Write-Warning 'The Ollama tray app is also running and may have respawned the server without these settings.'
        Write-Host   '   Quit it from the system tray, or set them for your account so any server picks them up:'
        Write-Host   "   [Environment]::SetEnvironmentVariable('OLLAMA_CONTEXT_LENGTH','$($env:OLLAMA_CONTEXT_LENGTH)','User')"
    }
}

# ── 2. Control Room server ──────────────────────────────────────────────
Write-Step 2 'Control Room server'
$logPath = Join-Path $env:TEMP 'qwen-harness-gui.log'

if (Test-Listening $Port) {
    Write-Host "Something is already listening on :$Port - reusing it."
} else {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $python) { $python = (Get-Command py -ErrorAction SilentlyContinue).Source }
    if (-not $python) { throw "Python 3.9+ is required. Run .\setup.ps1 first." }

    Start-Process -FilePath $python `
        -ArgumentList (Join-Path 'gui' 'server.py'), $Port `
        -WindowStyle Hidden `
        -RedirectStandardOutput $logPath `
        -RedirectStandardError "$logPath.err"
    for ($i = 0; $i -lt 20 -and -not (Test-Listening $Port); $i++) { Start-Sleep -Milliseconds 500 }
    Write-Host "GUI server started on :$Port (log: $logPath)"
}

# ── 3. Open ─────────────────────────────────────────────────────────────
$url = "http://127.0.0.1:$Port/gui/"
Write-Step 3 "Opening $url"

$healthy = $false
for ($i = 0; $i -lt 20; $i++) {
    try { if ((Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 1).ok) { $healthy = $true; break } } catch { }
    Start-Sleep -Milliseconds 500
}
if (-not $healthy) {
    Write-Warning "The backend is not answering /api/health. Check $logPath"
}

if (-not $NoBrowser) { Start-Process $url }

Write-Host "`nqwen-harness is awake -> $url" -ForegroundColor Green
Write-Host @"

Check it yourself:
  Invoke-RestMethod http://127.0.0.1:$Port/api/health
  Invoke-RestMethod http://127.0.0.1:$Port/api/platform
  Invoke-RestMethod http://127.0.0.1:11434/api/tags

Stop everything:
  Get-Process -Name python | Where-Object { `$_.Path -like '*python*' } | Stop-Process
  Stop-Process -Name ollama -Force
"@
