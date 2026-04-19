#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy.sh — pull latest changes and restart services
#
# Usage: ./deploy.sh
#
# Safe: never touches listings.db, config.json, or whatsapp-service/auth_data/
# ─────────────────────────────────────────────────────────────────────────────

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"

SCANNER_PLIST="$HOME/Library/LaunchAgents/com.nlrental.scanner.plist"
WA_PLIST="$HOME/Library/LaunchAgents/com.nlrental.whatsapp.plist"

echo ""
echo "=== NL Rental Scanner deploy ==="
echo ""

# ── 1. Pull latest code ───────────────────────────────────────────────────────
echo "→ Pulling latest changes..."
git -C "$SCRIPT_DIR" pull --ff-only origin main

# ── 2. Update dependencies (no-op if unchanged) ───────────────────────────────
echo "→ Syncing Python dependencies..."
"$VENV/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"

echo "→ Syncing Node dependencies..."
(cd "$SCRIPT_DIR/whatsapp-service" && npm install --silent)

# ── 3. Restart services ───────────────────────────────────────────────────────
echo "→ Restarting scanner..."
launchctl kickstart -k "gui/$(id -u)/com.nlrental.scanner"

echo "→ Restarting WhatsApp service..."
launchctl kickstart -k "gui/$(id -u)/com.nlrental.whatsapp"

# ── 4. Tail logs briefly to confirm clean startup ────────────────────────────
echo ""
echo "✓ Deploy complete. Tailing logs for 8 seconds..."
echo "  (Ctrl-C to stop watching — services keep running)"
echo ""
sleep 2
tail -n 5 "$SCRIPT_DIR/app.log" "$SCRIPT_DIR/whatsapp.log" 2>/dev/null || true
echo ""
echo "  Health: curl http://localhost:5001/api/health"
echo ""
