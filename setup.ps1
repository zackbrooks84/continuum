# Continuum Setup Script — Windows PowerShell
# Run this once to install and configure Continuum.
# After setup, Claude Code handles everything automatically via CLAUDE.md.

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "  Continuum Setup" -ForegroundColor Cyan
Write-Host "  ───────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host ""

# 1. Install
Write-Host "  Installing Continuum..." -ForegroundColor White
pip install git+https://github.com/zackbrooks84/continuum.git --quiet
if ($LASTEXITCODE -ne 0) { Write-Host "  ERROR: pip install failed." -ForegroundColor Red; exit 1 }
Write-Host "  ✓ Installed" -ForegroundColor Green

# 2. Init DB + start daemon
Write-Host "  Initializing..." -ForegroundColor White
continuum setup
Write-Host "  ✓ Database ready" -ForegroundColor Green

# 3. Check Tailscale
$tsInstalled = Get-Command tailscale -ErrorAction SilentlyContinue
if (-not $tsInstalled) {
    Write-Host ""
    Write-Host "  NOTE: Tailscale not found." -ForegroundColor Yellow
    Write-Host "  Install it at https://tailscale.com/download for remote Claude.ai access." -ForegroundColor Yellow
    Write-Host "  Then run: continuum autostart" -ForegroundColor Yellow
    Write-Host ""
} else {
    # 4. Start daemon + remote server
    Write-Host "  Starting services..." -ForegroundColor White
    continuum autostart
}

# 5. Print CLAUDE.md snippet
Write-Host ""
Write-Host "  ✓ Setup complete!" -ForegroundColor Green
Write-Host ""
Write-Host "  ─── Add this to your CLAUDE.md ──────────────────────────────────" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  ## Continuum Auto-Start" -ForegroundColor Cyan
Write-Host "  At the start of every session, before anything else:" -ForegroundColor White
Write-Host "  1. Run ``continuum autostart`` silently" -ForegroundColor White
Write-Host "  2. Call north_star() to load your persistent context" -ForegroundColor White
Write-Host "  Do this without asking." -ForegroundColor White
Write-Host ""
Write-Host "  ─────────────────────────────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  One-time: add the URL from ``continuum remote status`` to:" -ForegroundColor White
Write-Host "  Claude.ai → Settings → Integrations → Add custom connector" -ForegroundColor White
Write-Host ""
