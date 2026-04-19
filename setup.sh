#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# NL Rental Scanner — setup & service registration
#
# Run once on any machine to get everything going:
#   chmod +x setup.sh && ./setup.sh
#
# To migrate: copy this whole folder to the new machine, then run ./setup.sh
# The database (listings.db) and config.json travel with the folder.
# ─────────────────────────────────────────────────────────────────────────────

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
PLIST_LABEL="com.nlrental.scanner"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"

echo ""
echo "=== NL Rental Scanner setup ==="
echo "Project dir: $SCRIPT_DIR"
echo ""

# ── 1. Python virtual environment ─────────────────────────────────────────────
if [ ! -d "$VENV" ]; then
  echo "→ Creating virtual environment..."
  python3 -m venv "$VENV"
fi

echo "→ Installing Python dependencies..."
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"

# ── 2. Playwright browser ──────────────────────────────────────────────────────
echo "→ Installing Playwright Chromium..."
"$VENV/bin/playwright" install chromium

# ── 3. Node dependencies (WhatsApp service) ───────────────────────────────────
echo "→ Installing Node.js dependencies for whatsapp-service..."
(cd "$SCRIPT_DIR/whatsapp-service" && npm install --silent)

# ── 4. Config ─────────────────────────────────────────────────────────────────
if [ ! -f "$SCRIPT_DIR/config.json" ]; then
  echo "→ Creating default config.json — fill in email credentials before first run."
  cat > "$SCRIPT_DIR/config.json" <<'EOF'
{
  "scrape": {
    "cities": ["amsterdam", "amstelveen"]
  },
  "schedule": {
    "interval_minutes": 60
  },
  "notifications": {
    "whatsapp_number": "",
    "whatsapp_apikey": "",
    "email_from": "",
    "email_password": "",
    "whatsapp_service_url": "http://localhost:3001",
    "whatsapp_service_token": ""
  }
}
EOF
fi

mkdir -p "$HOME/Library/LaunchAgents"

# ── 5. launchd: Chrome with remote debugging ──────────────────────────────────
CHROME_PLIST_LABEL="com.nlrental.chrome"
CHROME_PLIST_PATH="$HOME/Library/LaunchAgents/$CHROME_PLIST_LABEL.plist"
CHROME_APP="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

echo "→ Registering Chrome (remote debug) service..."

if launchctl list "$CHROME_PLIST_LABEL" &>/dev/null; then
  launchctl unload "$CHROME_PLIST_PATH" 2>/dev/null || true
fi

cat > "$CHROME_PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$CHROME_PLIST_LABEL</string>

  <key>ProgramArguments</key>
  <array>
    <string>$CHROME_APP</string>
    <string>--remote-debugging-port=9222</string>
    <string>--profile-directory=Profile 1</string>
    <string>--no-first-run</string>
    <string>--no-default-browser-check</string>
    <string>--headless=new</string>
  </array>

  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>

  <key>StandardOutPath</key>
  <string>$SCRIPT_DIR/chrome.log</string>
  <key>StandardErrorPath</key>
  <string>$SCRIPT_DIR/chrome.log</string>
</dict>
</plist>
EOF

launchctl load "$CHROME_PLIST_PATH"

# ── 6. launchd: WhatsApp service ──────────────────────────────────────────────
WA_PLIST_LABEL="com.nlrental.whatsapp"
WA_PLIST_PATH="$HOME/Library/LaunchAgents/$WA_PLIST_LABEL.plist"
NODE_BIN="$(which node)"

echo "→ Registering WhatsApp service..."

if launchctl list "$WA_PLIST_LABEL" &>/dev/null; then
  launchctl unload "$WA_PLIST_PATH" 2>/dev/null || true
fi

cat > "$WA_PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$WA_PLIST_LABEL</string>

  <key>ProgramArguments</key>
  <array>
    <string>$NODE_BIN</string>
    <string>$SCRIPT_DIR/whatsapp-service/index.js</string>
  </array>

  <key>WorkingDirectory</key>
  <string>$SCRIPT_DIR/whatsapp-service</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>SCANNER_URL</key>
    <string>http://localhost:5001</string>
  </dict>

  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>

  <key>StandardOutPath</key>
  <string>$SCRIPT_DIR/whatsapp.log</string>
  <key>StandardErrorPath</key>
  <string>$SCRIPT_DIR/whatsapp.log</string>
</dict>
</plist>
EOF

launchctl load "$WA_PLIST_PATH"

# ── 7. launchd: Scanner app (auto-start + auto-restart) ───────────────────────
echo "→ Registering scanner service..."

if launchctl list "$PLIST_LABEL" &>/dev/null; then
  launchctl unload "$PLIST_PATH" 2>/dev/null || true
fi

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$PLIST_LABEL</string>

  <key>ProgramArguments</key>
  <array>
    <string>$VENV/bin/python</string>
    <string>$SCRIPT_DIR/app.py</string>
  </array>

  <key>WorkingDirectory</key>
  <string>$SCRIPT_DIR</string>

  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>

  <key>StandardOutPath</key>
  <string>$SCRIPT_DIR/app.log</string>
  <key>StandardErrorPath</key>
  <string>$SCRIPT_DIR/app.log</string>
</dict>
</plist>
EOF

launchctl load "$PLIST_PATH"

# ── 8. launchd: Watchdog (runs every hour) ────────────────────────────────────
WD_PLIST_LABEL="com.nlrental.watchdog"
WD_PLIST_PATH="$HOME/Library/LaunchAgents/$WD_PLIST_LABEL.plist"

echo "→ Registering watchdog service..."

if launchctl list "$WD_PLIST_LABEL" &>/dev/null; then
  launchctl unload "$WD_PLIST_PATH" 2>/dev/null || true
fi

cat > "$WD_PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$WD_PLIST_LABEL</string>

  <key>ProgramArguments</key>
  <array>
    <string>$VENV/bin/python</string>
    <string>$SCRIPT_DIR/watchdog.py</string>
  </array>

  <key>WorkingDirectory</key>
  <string>$SCRIPT_DIR</string>

  <key>StartInterval</key>
  <integer>3600</integer>

  <key>StandardOutPath</key>
  <string>$SCRIPT_DIR/watchdog.log</string>
  <key>StandardErrorPath</key>
  <string>$SCRIPT_DIR/watchdog.log</string>
</dict>
</plist>
EOF

launchctl load "$WD_PLIST_PATH"

# ── 9. Log rotation (newsyslog) ───────────────────────────────────────────────
echo "→ Configuring log rotation..."
NEWSYSLOG_CONF="/etc/newsyslog.d/nlrental.conf"

# newsyslog format: path [owner:group] mode count size when flags
sudo tee "$NEWSYSLOG_CONF" > /dev/null <<EOF
# NL Rental Scanner — rotate logs at 10 MB, keep 5 archives
$SCRIPT_DIR/app.log      644  5  10240  *  J
$SCRIPT_DIR/scanner.log  644  5  10240  *  J
$SCRIPT_DIR/chrome.log    644  5  10240  *  J
$SCRIPT_DIR/whatsapp.log  644  5  10240  *  J
$SCRIPT_DIR/watchdog.log  644  5  10240  *  J
EOF

echo ""
echo "✓ Done. All services are running."
echo ""
echo "  UI:              http://localhost:5001"
echo "  Health check:    http://localhost:5001/api/health"
echo "  WhatsApp QR:     http://localhost:3001/qr"
echo ""
echo "  App logs:        tail -f $SCRIPT_DIR/app.log"
echo "  Scanner logs:    tail -f $SCRIPT_DIR/scanner.log"
echo "  WhatsApp logs:   tail -f $SCRIPT_DIR/whatsapp.log"
echo "  Chrome logs:     tail -f $SCRIPT_DIR/chrome.log"
echo ""
echo "  Watchdog logs:   tail -f $SCRIPT_DIR/watchdog.log
  Deploy update:   ./deploy.sh"
echo ""
echo "  Stop all:   launchctl unload $PLIST_PATH $WA_PLIST_PATH $CHROME_PLIST_PATH $WD_PLIST_PATH"
echo "  Start all:  launchctl load   $PLIST_PATH $WA_PLIST_PATH $CHROME_PLIST_PATH $WD_PLIST_PATH"
echo ""
