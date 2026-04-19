# NL Rental Scanner

Scrapes Pararius and Funda for rental listings and notifies subscribers via WhatsApp and email. Supports per-subscriber filters, voice message updates, and student-listing classification.

---

## Requirements

- macOS (tested on macOS 14+)
- Python 3.12+
- Node.js 18+
- Google Chrome installed at `/Applications/Google Chrome.app`
- An OpenAI API key (for voice transcription and filter merging)
- A Gmail account with an [App Password](https://myaccount.google.com/apppasswords)

---

## First-time setup (Mac Mini or any new machine)

### 1. Copy the project folder to the new machine

The database, config, and WhatsApp session travel with the folder. The simplest way:

```bash
rsync -av --exclude='.venv' --exclude='node_modules' \
  /path/to/new-listings/ user@mac-mini:/path/to/new-listings/
```

Or clone from git and restore `listings.db` and `config.json` separately (they are gitignored).

### 2. Configure Mac system settings (do once, manually)

- **Energy Saver** → disable all sleep, enable "Start up automatically after a power failure"
- **Users & Groups** → enable auto-login for your account
- **Sharing** → enable Remote Login (SSH)

### 3. Create `config.json`

Copy the example and fill in your credentials:

```bash
cp config.example.json config.json
```

```json
{
  "openai_api_key": "sk-...",
  "scrape": {
    "cities": ["amsterdam", "amstelveen"]
  },
  "schedule": {
    "interval_minutes": 60
  },
  "notifications": {
    "email_from": "you@gmail.com",
    "email_password": "<gmail-app-password>",
    "whatsapp_number": "+31...",
    "whatsapp_apikey": "",
    "whatsapp_service_url": "http://localhost:3001",
    "whatsapp_service_token": ""
  }
}
```

### 4. Run setup

```bash
chmod +x setup.sh && ./setup.sh
```

This will:
- Create a Python virtual environment and install dependencies
- Install Playwright Chromium
- Install Node.js dependencies for the WhatsApp service
- Register four launchd agents (auto-start on login, auto-restart on crash):
  - `com.nlrental.scanner` — Flask UI + scan scheduler (port 5001)
  - `com.nlrental.whatsapp` — WhatsApp microservice (port 3001)
  - `com.nlrental.chrome` — headless Chrome for Funda scraping
  - `com.nlrental.watchdog` — hourly health check, emails you on failure
- Configure log rotation via newsyslog (10 MB cap, 5 archives per log)

### 5. Pair WhatsApp

On first run, open the QR page in a browser on the same machine (or via SSH tunnel):

```bash
open http://localhost:3001/qr
```

Scan with WhatsApp → Linked Devices → Link a Device. The session is saved to `whatsapp-service/auth_data/` and persists across restarts. You should only need to do this once.

---

## Daily operations

### Web UI

```
http://localhost:5001
```

Manage subscribers, queries, scan history, and config. Accessible from any device on the same network.

### Health check

```bash
curl http://localhost:5001/api/health
```

Returns scheduler state, time of last scan, and WhatsApp connection status. The watchdog calls this automatically every hour.

### Logs

```bash
tail -f app.log       # Flask + scheduler
tail -f scanner.log   # Scraping + notifications (rotated, 10 MB max)
tail -f whatsapp.log  # WhatsApp service
tail -f watchdog.log  # Hourly health checks
```

### Service control

```bash
# Restart a single service
launchctl kickstart -k gui/$(id -u)/com.nlrental.scanner
launchctl kickstart -k gui/$(id -u)/com.nlrental.whatsapp

# Stop everything
launchctl unload \
  ~/Library/LaunchAgents/com.nlrental.scanner.plist \
  ~/Library/LaunchAgents/com.nlrental.whatsapp.plist \
  ~/Library/LaunchAgents/com.nlrental.chrome.plist \
  ~/Library/LaunchAgents/com.nlrental.watchdog.plist

# Start everything
launchctl load \
  ~/Library/LaunchAgents/com.nlrental.scanner.plist \
  ~/Library/LaunchAgents/com.nlrental.whatsapp.plist \
  ~/Library/LaunchAgents/com.nlrental.chrome.plist \
  ~/Library/LaunchAgents/com.nlrental.watchdog.plist
```

---

## Deploying updates

After pushing changes to `main` on your development machine:

```bash
# On the Mac Mini
./deploy.sh
```

This pulls from `main`, syncs Python and Node dependencies if needed, restarts the scanner and WhatsApp service, and tails logs briefly to confirm a clean startup.

`listings.db`, `config.json`, and `whatsapp-service/auth_data/` are never touched by deploy or git.

---

## Persistent state

| File | What it holds | Gitignored |
|---|---|---|
| `listings.db` | All seen listings, scan history, subscribers, queries | Yes |
| `config.json` | API keys, email credentials, schedule | Yes |
| `whatsapp-service/auth_data/` | WhatsApp Web session (never delete) | Yes |

These three must be present on any machine running the scanner. Back them up before migrating.

---

## Debugging remotely

SSH into the Mac Mini and tail logs:

```bash
ssh mac-mini "tail -f ~/path/to/new-listings/scanner.log"
```

To access the web UI or WhatsApp QR page from your laptop via SSH tunnel:

```bash
ssh -L 5001:localhost:5001 -L 3001:localhost:3001 mac-mini
# Then open http://localhost:5001 and http://localhost:3001/qr locally
```
