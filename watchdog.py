#!/usr/bin/env python3
"""
Watchdog — alerts via email if the scanner hasn't run in 2× its expected interval.

Run once per hour via launchd (com.nlrental.watchdog).
setup.sh installs it automatically alongside the main services.
"""

import json
import os
import smtplib
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")


def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)


def send_alert(cfg: dict, subject: str, body: str):
    notif = cfg.get("notifications", {})
    email_from = notif.get("email_from", "")
    email_password = notif.get("email_password", "")
    if not email_from or not email_password:
        print(f"[watchdog] ALERT (no email configured): {subject}")
        return
    msg = MIMEText(body)
    msg["Subject"] = f"[NL Rental] {subject}"
    msg["From"]    = email_from
    msg["To"]      = email_from
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(email_from, email_password)
        s.send_message(msg)
    print(f"[watchdog] Alert sent: {subject}")


def check_scanner(cfg: dict) -> list[str]:
    problems = []
    interval = int(cfg.get("schedule", {}).get("interval_minutes", 60))
    threshold = timedelta(minutes=interval * 2)

    try:
        req = urllib.request.Request("http://localhost:5001/api/health")
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
    except Exception as e:
        problems.append(f"Scanner UI unreachable: {e}")
        return problems

    last_scan = data.get("last_scan_at")
    if last_scan:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(last_scan.replace("Z", "+00:00"))
        if age > threshold:
            problems.append(
                f"Last scan was {int(age.total_seconds() // 60)} min ago "
                f"(threshold: {int(threshold.total_seconds() // 60)} min)"
            )
    else:
        problems.append("No scans recorded yet — scanner may not have run.")

    wa = data.get("whatsapp", {})
    if not wa.get("reachable"):
        problems.append("WhatsApp service is unreachable.")
    elif not wa.get("connected") and not wa.get("waiting_for_qr"):
        problems.append("WhatsApp service is reachable but not connected.")

    return problems


def main():
    try:
        cfg = load_config()
    except Exception as e:
        print(f"[watchdog] Cannot load config: {e}")
        sys.exit(1)

    problems = check_scanner(cfg)

    if problems:
        body = "\n".join(f"• {p}" for p in problems)
        body += f"\n\nChecked at: {datetime.now(timezone.utc).isoformat()}"
        body += "\n\nHealth: http://localhost:5001/api/health"
        send_alert(cfg, "Service problem detected", body)
        sys.exit(1)
    else:
        print(f"[watchdog] OK at {datetime.now(timezone.utc).isoformat()}")


if __name__ == "__main__":
    main()
