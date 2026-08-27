<#
.SYNOPSIS
    Qwen Harness — one-shot setup for Windows.

.DESCRIPTION
    The Windows counterpart of setup.sh. Checks for Ollama, starts it, pulls
    only the models that are genuinely missing, installs the Python
    dependencies, and installs Playwright's Chromium.

    setup.ps1 is a first-run installer. wake-up.ps1 is the every-day launch.

.PARAMETER SkipModels
    Install the Python side only. Useful when the models are already pulled and
    you just changed requirements.txt.

.PARAMETER SkipBrowsers
    Skip the Playwright Chromium download (~150 MB). scrape_url then cannot
    render JavaScript pages, and it will say so rather than pretending.

.EXAMPLE
    .\setup.ps1
    .\setup.ps1 -SkipModels
#>
[CmdletBinding()]
param(
    [switch]$SkipModels,
    [switch]$SkipBrowsers
)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSCommandPath)

function Write-Step($n, $text) {
    Write-Host "`n── $n. $text " -ForegroundColor Cyan -NoNewline
    Write-Host ('─' * [Math]::Max(0, 52 - $text.Length)) -ForegroundColor DarkCyan
}

# ── 1. Ollama ───────────────────────────────────────────────────────────
Write-Step 1 'Checking for Ollama'

# Ollama installs per-user under %LOCALAPPDATA%\Programs\Ollama, which is not
# always on PATH for a non-interactive or shortcut-launched process. Look
# there explicitly rather than reporting a working install as missing.
$ollamaCandidates = @(
    (Get-Command ollama -ErrorAction SilentlyContinue).Source,
    (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'),
    (Join-Path $env:ProgramFiles 'Ollama\ollama.exe')
) | Where-Object { $_ -and (Test-Path $_) }
$ollama = $ollamaCandidates | Select-Object -First 1

if (-not $ollama) {
    Write-Host 'Ollama not found.' -ForegroundColor Yellow
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host 'Installing via winget…'
        winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements
        $ollama = (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe')
    } else {
        throw "Install Ollama from https://ollama.com/download/windows, then re-run this script."
    }
}
if (-not (Test-Path $ollama)) { throw "Ollama still not found after install. Open a new terminal and re-run." }
Write-Host "Ollama: $ollama"
Write-Host (& $ollama --version)

# ── 2. Start the server ─────────────────────────────────────────────────
Write-Step 2 'Starting the Ollama server'
# Ollama binds 127.0.0.1 only, and `localhost` resolves to ::1 FIRST on
# Windows 11. Every probe below therefore uses the literal IPv4 address:
# measured, a `localhost` probe with -TimeoutSec 1 fails in 1083 ms (the ::1
# attempt eats the whole budget before the IPv4 fallback), while the same
# probe against 127.0.0.1 answers in 146 ms. The symptom was wake-up.ps1
# reporting "Ollama did not come up" every single run about a server that was
# already listening and serving.
$listening = $false
try {
    Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3 | Out-Null
    $listening = $true
} catch { $listening = $false }

if ($listening) {
    Write-Host 'Ollama already listening on :11434'
} else {
    Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        try { Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3 | Out-Null; break } catch { }
    }
    Write-Host 'Ollama started'
}

# ── 3. Model roster ─────────────────────────────────────────────────────
# `ollama pull` on an existing model still re-verifies every layer against the
# registry, so guard on what is already installed and skip it outright —
# re-running setup on a provisioned machine should download nothing.
if ($SkipModels) {
    Write-Step 3 'Model roster (skipped)'
} else {
    Write-Step 3 'Model roster'
    $installed = @()
    try { $installed = (Invoke-RestMethod 'http://127.0.0.1:11434/api/tags').models.name } catch { }

    $roster = [ordered]@{
        'qwen3:4b'     = '~2.5 GB - primary model'
        'qwen3:0.6b'   = '~0.5 GB - lightest agent'
        'gemma3:1b'    = '~0.8 GB - light agent'
        'llama3.2:1b'  = '~1.3 GB - light agent'
        'gemma2:2b'    = '~1.6 GB - light agent'
    }
    foreach ($name in $roster.Keys) {
        if ($installed -contains $name -or $installed -contains "${name}:latest") {
            Write-Host "  OK  $name already installed - skipping   ($($roster[$name]))" -ForegroundColor DarkGray
        } else {
            Write-Host "  ->  Pulling $name   ($($roster[$name]))"
            & $ollama pull $name
            if ($LASTEXITCODE -ne 0) { Write-Warning "pull failed for $name (exit $LASTEXITCODE)" }
        }
    }
}

# ── 4. Python dependencies ──────────────────────────────────────────────
Write-Step 4 'Python dependencies'

# Deliberately NOT "python3": on Windows that name resolves to a Microsoft
# Store redirector stub under WindowsApps, which exits 9009 and opens the
# Store instead of running anything.
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $python) { throw "Python 3.9+ is required. Install it from https://python.org/downloads (tick 'Add to PATH')." }
Write-Host "python: $python"
& $python --version

# --break-system-packages does not exist before pip 23 and is a hard error on
# older pips. It is also rarely needed on Windows, where python.org installs
# are not PEP 668 "externally managed" — but a Microsoft Store Python is, so
# detect support rather than assuming either way.
$pipFlags = @()
$helpText = & $python -m pip install --help 2>&1 | Out-String
if ($helpText -match '--break-system-packages') {
    $externallyManaged = & $python -c "import os,sysconfig; print(os.path.exists(os.path.join(sysconfig.get_path('stdlib'),'EXTERNALLY-MANAGED')))"
    if ($externallyManaged.Trim() -eq 'True') {
        $pipFlags += '--break-system-packages'
        Write-Host '   (externally-managed interpreter - using --break-system-packages)'
    }
}

& $python -m pip install @pipFlags -r (Join-Path $PWD 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw "pip install failed ($LASTEXITCODE)" }

# ── 5. Playwright browsers ──────────────────────────────────────────────
if ($SkipBrowsers) {
    Write-Step 5 'Playwright browsers (skipped)'
    Write-Warning 'scrape_url cannot render JavaScript pages without Chromium. It will report that rather than guessing.'
} else {
    Write-Step 5 'Playwright browsers'
    & $python -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { Write-Warning "playwright install failed ($LASTEXITCODE) - headless rendering will be unavailable" }
}

# ── 6. Verify ───────────────────────────────────────────────────────────
Write-Step 6 'Verifying'
& $python -c "from tools.datetime_tool import get_datetime; r = get_datetime(); print('  date_time  ->', r.get('spoken', r))"
& $python -c "from tools.context_budget import total_ram_gb, recommend_context; r = recommend_context(4.0, None); print(f'  context    -> qwen3:4b at {r[\"context\"]} tokens on {total_ram_gb():.0f} GB RAM')"
& $python -c "from tools.sandbox import confinement; print('  sandbox    ->', confinement()['mode'])"

Write-Host "`nSetup complete." -ForegroundColor Green
Write-Host @"

Quick start:
  .\wake-up.ps1                      launch the harness in your browser
  .\script\build_and_run.ps1         launch the native desktop app
  python examples\01_hello.py        the raw Python examples
"@
