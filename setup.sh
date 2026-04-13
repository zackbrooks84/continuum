#!/usr/bin/env bash
# Continuum Setup Script — macOS / Linux
# Run this once to install and configure Continuum.
# After setup, Claude Code handles everything automatically via CLAUDE.md.

set -e

echo ""
echo "  Continuum Setup"
echo "  ───────────────────────────────────────────"
echo ""

# 1. Install
echo "  Installing Continuum..."
pip install git+https://github.com/zackbrooks84/continuum.git --quiet
echo "  ✓ Installed"

# 2. Init DB + start daemon
echo "  Initializing..."
continuum setup
echo "  ✓ Database ready"

# 3. Check Tailscale
if ! command -v tailscale &> /dev/null; then
    echo ""
    echo "  NOTE: Tailscale not found."
    echo "  Install it at https://tailscale.com/download for remote Claude.ai access."
    echo "  Then run: continuum autostart"
    echo ""
else
    # 4. Start daemon + remote server
    echo "  Starting services..."
    continuum autostart
fi

# 5. Print CLAUDE.md snippet
echo ""
echo "  ✓ Setup complete!"
echo ""
echo "  ─── Add this to your CLAUDE.md ──────────────────────────────────"
echo ""
echo "  ## Continuum Auto-Start"
echo "  At the start of every session, before anything else:"
echo "  1. Run \`continuum autostart\` silently"
echo "  2. Call north_star() to load your persistent context"
echo "  Do this without asking."
echo ""
echo "  ─────────────────────────────────────────────────────────────────"
echo ""
echo "  One-time: add the URL from \`continuum remote status\` to:"
echo "  Claude.ai → Settings → Integrations → Add custom connector"
echo ""
