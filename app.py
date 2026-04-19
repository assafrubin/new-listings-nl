#!/usr/bin/env python3
"""Local web UI for the NL rental scanner. Run with: python app.py"""

import os
import sys
import threading
import subprocess
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template_string, request, redirect, url_for, jsonify
import db

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
SCANNER_PATH = os.path.join(BASE_DIR, "scanner.py")

app = Flask(__name__)

# ── Scheduler state ───────────────────────────────────────────────────────────

_state = {
    "running": False,
    "next_run_at": None,   # UTC ISO string
    "started_at": None,
}
_timer = None
_state_lock = threading.Lock()


def _load_interval() -> int:
    """Return scan interval in minutes from config (default 60)."""
    import json as _json
    try:
        cfg = _json.load(open(CONFIG_FILE))
        return int(cfg.get("schedule", {}).get("interval_minutes", 60))
    except Exception:
        return 60


def _save_interval(minutes: int):
    import json as _json
    try:
        with open(CONFIG_FILE) as f:
            cfg = _json.load(f)
    except Exception:
        cfg = {}
    cfg.setdefault("schedule", {})["interval_minutes"] = minutes
    with open(CONFIG_FILE, "w") as f:
        _json.dump(cfg, f, indent=2)


def _do_run():
    global _timer
    with _state_lock:
        _state["running"] = True
        _state["started_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Use the same Python interpreter that's running app.py (venv-aware)
    try:
        subprocess.run([sys.executable, SCANNER_PATH], cwd=BASE_DIR)
    finally:
        with _state_lock:
            _state["running"] = False
            _state["started_at"] = None
        _schedule_next()


def _schedule_next(delay_seconds: int = None):
    global _timer
    if _timer is not None:
        _timer.cancel()
    if delay_seconds is None:
        delay_seconds = _load_interval() * 60
    next_run = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
    with _state_lock:
        _state["next_run_at"] = next_run.strftime("%Y-%m-%dT%H:%M:%SZ")
    _timer = threading.Timer(delay_seconds, _do_run)
    _timer.daemon = True
    _timer.start()


def start_scheduler():
    _schedule_next()

# ── Shared layout ──────────────────────────────────────────────────────────────

BASE_STYLE = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: #f0f2f5;
  color: #1a1a2e;
  min-height: 100vh;
  display: flex;
}
.sidebar {
  width: 220px;
  min-height: 100vh;
  background: #1a1a2e;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  position: fixed;
  top: 0; left: 0; bottom: 0;
}
.sidebar .brand {
  padding: 28px 24px 20px;
  font-size: 1rem;
  font-weight: 700;
  color: #fff;
  text-decoration: none;
  display: block;
  border-bottom: 1px solid rgba(255,255,255,.08);
  margin-bottom: 12px;
}
.sidebar .brand span { color: #e84e1b; }
.sidebar nav {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 0 12px;
}
.sidebar nav a {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 8px;
  font-size: .88rem;
  color: rgba(255,255,255,.55);
  text-decoration: none;
  transition: background .15s, color .15s;
}
.sidebar nav a:hover { background: rgba(255,255,255,.07); color: #fff; }
.sidebar nav a.active { background: rgba(232,78,27,.18); color: #ff7a52; font-weight: 600; }
.sidebar nav a .icon { font-size: 1rem; width: 20px; text-align: center; flex-shrink: 0; }
.main {
  margin-left: 220px;
  flex: 1;
  padding: 36px 40px;
  max-width: 900px;
}
h2 { font-size: 1.25rem; font-weight: 700; margin-bottom: 20px; }
"""

def nav_html(active: str) -> str:
    def link(label, icon, endpoint):
        cls = 'active' if active == label else ''
        return (f'<a href="{url_for(endpoint)}" class="{cls}">'
                f'<span class="icon">{icon}</span>{label}</a>')
    return f"""
    <div class="sidebar">
      <a class="brand" href="/">NL Rental <span>Scanner</span></a>
      <nav>
        {link('Scanner', '📋', 'index')}
        {link('Subscribers', '👥', 'subscribers')}
      </nav>
    </div>
    """

# ── Filters ───────────────────────────────────────────────────────────────────

def format_time(iso: str) -> str:
    from datetime import datetime, timezone, timedelta
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        amsterdam = dt + timedelta(hours=2)
        return amsterdam.strftime("%d %b %Y  %H:%M")
    except Exception:
        return iso

app.jinja_env.filters["format_time"] = format_time

# ── Scan History ──────────────────────────────────────────────────────────────

HISTORY_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Scanner — NL Rental Scanner</title>
  <style>
    {{ style | safe }}
    .scope-bar {
      background: #fff;
      border-radius: 12px;
      padding: 16px 20px;
      margin-bottom: 20px;
      box-shadow: 0 1px 4px rgba(0,0,0,.07);
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }
    .scope-label { font-size: .8rem; font-weight: 600; color: #6b7280; text-transform: uppercase; letter-spacing: .04em; }
    .city-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 12px;
      background: #f3f4f6;
      border-radius: 99px;
      font-size: .82rem;
      font-weight: 500;
      color: #1a1a2e;
    }
    .scope-edit-btn {
      margin-left: auto;
      padding: 5px 14px;
      border: 1px solid #e5e7eb;
      border-radius: 8px;
      background: transparent;
      font-size: .82rem;
      color: #6b7280;
      cursor: pointer;
    }
    .scope-edit-btn:hover { border-color: #e84e1b; color: #e84e1b; }
    .scope-form {
      display: none;
      margin-top: 14px;
      padding-top: 14px;
      border-top: 1px solid #f0f2f5;
      width: 100%;
    }
    .scope-form.open { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
    .scope-form input {
      flex: 1;
      min-width: 200px;
      padding: 8px 12px;
      border: 1px solid #d1d5db;
      border-radius: 8px;
      font-size: .88rem;
      outline: none;
    }
    .scope-form input:focus { border-color: #e84e1b; }
    .scope-form .save-btn {
      padding: 8px 18px;
      background: #e84e1b;
      color: #fff;
      border: none;
      border-radius: 8px;
      font-size: .88rem;
      font-weight: 600;
      cursor: pointer;
    }
    .scope-form .save-btn:hover { background: #c94016; }
    .scope-hint { font-size: .75rem; color: #9ca3af; width: 100%; margin-top: 2px; }

    /* Schedule bar */
    .schedule-bar {
      background: #fff;
      border-radius: 12px;
      padding: 14px 20px;
      margin-bottom: 20px;
      box-shadow: 0 1px 4px rgba(0,0,0,.07);
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }
    .sched-label { font-size: .8rem; font-weight: 700; text-transform: uppercase; letter-spacing: .04em; color: #6b7280; }
    .sched-status {
      display: flex; align-items: center; gap: 7px;
      font-size: .88rem; font-weight: 500;
    }
    .dot-running {
      width: 8px; height: 8px; border-radius: 50%;
      background: #22c55e;
      box-shadow: 0 0 0 2px rgba(34,197,94,.25);
      animation: pulse 1.4s infinite;
    }
    @keyframes pulse {
      0%, 100% { opacity: 1; } 50% { opacity: .4; }
    }
    .dot-idle { width: 8px; height: 8px; border-radius: 50%; background: #d1d5db; }
    .run-now-btn {
      margin-left: auto;
      padding: 7px 16px;
      background: #e84e1b;
      color: #fff;
      border: none;
      border-radius: 8px;
      font-size: .85rem;
      font-weight: 600;
      cursor: pointer;
    }
    .run-now-btn:hover { background: #c94016; }
    .run-now-btn:disabled { background: #d1d5db; cursor: not-allowed; }
    .sched-edit-btn {
      padding: 5px 12px;
      border: 1px solid #e5e7eb;
      border-radius: 8px;
      background: transparent;
      font-size: .8rem;
      color: #6b7280;
      cursor: pointer;
    }
    .sched-edit-btn:hover { border-color: #e84e1b; color: #e84e1b; }
    .sched-form {
      display: none;
      width: 100%;
      padding-top: 12px;
      margin-top: 4px;
      border-top: 1px solid #f0f2f5;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .sched-form.open { display: flex; }
    .sched-form input {
      width: 100px;
      padding: 7px 10px;
      border: 1px solid #d1d5db;
      border-radius: 8px;
      font-size: .88rem;
      outline: none;
    }
    .sched-form input:focus { border-color: #e84e1b; }
    .sched-form .save-btn {
      padding: 7px 16px;
      background: #e84e1b;
      color: #fff;
      border: none;
      border-radius: 8px;
      font-size: .85rem;
      font-weight: 600;
      cursor: pointer;
    }
    .run {
      background: #fff;
      border-radius: 12px;
      margin-bottom: 16px;
      overflow: hidden;
      box-shadow: 0 1px 4px rgba(0,0,0,.07);
    }
    .run-header {
      display: flex;
      align-items: center;
      gap: 14px;
      padding: 16px 20px;
      cursor: pointer;
      user-select: none;
    }
    .run-header:hover { background: #fafafa; }
    .badge {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 32px; height: 32px;
      border-radius: 50%;
      font-weight: 700;
      font-size: .85rem;
      flex-shrink: 0;
    }
    .badge-new  { background: #e84e1b; color: #fff; }
    .badge-none { background: #e5e7eb; color: #6b7280; }
    .run-meta { flex: 1; }
    .run-time  { font-size: .8rem; color: #6b7280; margin-top: 2px; }
    .run-label { font-weight: 600; font-size: .95rem; }
    .chevron { font-size: .8rem; color: #9ca3af; transition: transform .2s; }
    .run.open .chevron { transform: rotate(90deg); }
    .listings {
      display: none;
      padding: 0 20px 16px;
      border-top: 1px solid #f0f2f5;
    }
    .run.open .listings { display: block; }
    .listing-card {
      display: flex;
      align-items: flex-start;
      gap: 12px;
      padding: 14px 0;
      border-bottom: 1px solid #f0f2f5;
    }
    .listing-card:last-child { border-bottom: none; }
    .source-badge {
      padding: 3px 8px;
      border-radius: 6px;
      font-size: .72rem;
      font-weight: 700;
      text-transform: uppercase;
      flex-shrink: 0;
      margin-top: 2px;
    }
    .source-pararius { background: #fef3c7; color: #92400e; }
    .source-funda    { background: #dbeafe; color: #1e40af; }
    .listing-info { flex: 1; }
    .listing-title { font-weight: 600; font-size: .95rem; }
    .listing-title a { color: #1a1a2e; text-decoration: none; }
    .listing-title a:hover { text-decoration: underline; color: #e84e1b; }
    .listing-price { color: #e84e1b; font-weight: 700; font-size: .9rem; margin-top: 3px; }
    .listing-details { font-size: .82rem; color: #6b7280; margin-top: 4px; }
    .empty-state { text-align: center; padding: 80px 20px; color: #9ca3af; }
    .empty-state p { margin-top: 8px; font-size: .85rem; }
  </style>
</head>
<body>
  {{ nav | safe }}
  <div class="main">
    <h2>Scanner</h2>

    <!-- Scope bar -->
    <div class="scope-bar">
      <span class="scope-label">Scope</span>
      {% for city in cities %}
        <span class="city-pill">🏙 {{ city | title }}</span>
      {% endfor %}
      <button class="scope-edit-btn" onclick="toggleScopeForm()">Edit cities</button>
      <form class="scope-form" id="scopeForm" method="POST" action="/scope">
        <input type="text" name="cities"
               value="{{ cities | join(', ') }}"
               placeholder="amsterdam, amstelveen, ...">
        <button type="submit" class="save-btn">Save</button>
        <span class="scope-hint">Comma-separated slugs as used in pararius.nl URLs — e.g. <em>amsterdam, den-haag, den-bosch</em></span>
      </form>
    </div>

    <!-- Schedule bar -->
    <div class="schedule-bar" id="scheduleBar">
      <span class="sched-label">Schedule</span>
      <div class="sched-status" id="schedStatus">
        {% if state.running %}
          <div class="dot-running"></div> Running now&hellip;
        {% else %}
          <div class="dot-idle" id="statusDot"></div>
          <span id="statusText">
            Every {{ interval }} min &mdash; next run <span id="countdown"></span>
          </span>
        {% endif %}
      </div>
      <button class="sched-edit-btn" onclick="toggleSchedForm()">Edit interval</button>
      <button class="run-now-btn" id="runBtn"
              {% if state.running %}disabled{% endif %}
              onclick="triggerRun()">▶ Run now</button>
      <form class="sched-form" id="schedForm" method="POST" action="/schedule">
        <label style="font-size:.82rem;font-weight:600;color:#374151">Every</label>
        <input type="number" name="interval_minutes" value="{{ interval }}" min="1">
        <label style="font-size:.82rem;color:#6b7280">minutes</label>
        <button type="submit" class="save-btn">Save</button>
      </form>
    </div>

    {% if not runs %}
      <div class="empty-state">
        No scan runs yet.
        <p>Run <code>python scanner.py</code> to start.</p>
      </div>
    {% else %}
      {% for run in runs %}
        <div class="run {% if run.new_count > 0 %}open{% endif %}" onclick="toggle(this)">
          <div class="run-header">
            <div class="badge {% if run.new_count > 0 %}badge-new{% else %}badge-none{% endif %}">
              {{ run.new_count }}
            </div>
            <div class="run-meta">
              <div class="run-label">
                {% if run.new_count > 0 %}
                  {{ run.new_count }} new listing{{ 's' if run.new_count != 1 else '' }} found
                {% else %}
                  No new listings
                {% endif %}
              </div>
              <div class="run-time">{{ run.ran_at | format_time }}</div>
            </div>
            {% if run.new_count > 0 %}<span class="chevron">▶</span>{% endif %}
          </div>

          {% if run.new_count > 0 %}
          <div class="listings">
            {% for l in run.listings %}
              <div class="listing-card">
                <span class="source-badge source-{{ l.source | lower }}">{{ l.source }}</span>
                <div class="listing-info">
                  <div class="listing-title">
                    <a href="{{ l.url }}" target="_blank">
                      {% if l.title and not l.title.startswith('http') %}
                        {{ l.title }}
                      {% else %}
                        {# Funda fallback: extract readable address from URL slug #}
                        {% set parts = l.url.rstrip('/').split('/') %}
                        {% set slug = parts[-2] if parts[-1].isdigit() else parts[-1] %}
                        {{ slug | replace('-', ' ') | title }}
                      {% endif %}
                    </a>
                  </div>
                  <div class="listing-price">{{ l.price }}</div>
                  {% set parts = [] %}
                  {% if l.size %}{% set _ = parts.append(l.size) %}{% endif %}
                  {% if l.rooms %}{% set _ = parts.append(l.rooms ~ ' rooms') %}{% endif %}
                  {% if l.energy %}{% set _ = parts.append(l.energy) %}{% endif %}
                  {% if l.agency %}{% set _ = parts.append(l.agency) %}{% endif %}
                  {% if l.phone %}{% set _ = parts.append('☎ ' ~ l.phone) %}{% endif %}
                  {% if parts %}
                    <div class="listing-details">{{ parts | join('  ·  ') }}</div>
                  {% endif %}
                </div>
              </div>
            {% endfor %}
          </div>
          {% endif %}
        </div>
      {% endfor %}
    {% endif %}
  </div>

  <script>
    function toggle(el) {
      if (!el.querySelector('.listings')) return;
      el.classList.toggle('open');
    }
    function toggleScopeForm() {
      document.getElementById('scopeForm').classList.toggle('open');
    }
    function toggleSchedForm() {
      document.getElementById('schedForm').classList.toggle('open');
    }

    // ── Countdown + live status ──────────────────────────────────────────────
    const NEXT_RUN_AT = "{{ state.next_run_at or '' }}";
    let _running = {{ 'true' if state.running else 'false' }};

    function formatCountdown(ms) {
      if (ms <= 0) return 'any moment';
      const s = Math.floor(ms / 1000);
      const m = Math.floor(s / 60);
      const h = Math.floor(m / 60);
      if (h > 0) return `in ${h}h ${m % 60}m`;
      if (m > 0) return `in ${m}m ${s % 60}s`;
      return `in ${s}s`;
    }

    function updateCountdown() {
      const el = document.getElementById('countdown');
      if (!el || !NEXT_RUN_AT) return;
      const diff = new Date(NEXT_RUN_AT) - Date.now();
      // add local time hint
      const local = new Date(NEXT_RUN_AT).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
      el.textContent = formatCountdown(diff) + ` (${local})`;
    }

    function applyStatus(data) {
      const wasRunning = _running;
      _running = data.running;
      const statusEl = document.getElementById('schedStatus');
      const runBtn   = document.getElementById('runBtn');
      if (!statusEl) return;

      if (data.running) {
        statusEl.innerHTML = '<div class="dot-running"></div> Running now&hellip;';
        if (runBtn) runBtn.disabled = true;
      } else {
        const local = data.next_run_at
          ? new Date(data.next_run_at).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})
          : '—';
        statusEl.innerHTML =
          '<div class="dot-idle"></div>' +
          '<span>Every {{ interval }} min &mdash; next run ' +
          '<span id="countdown"></span></span>';
        if (runBtn) runBtn.disabled = false;
        updateCountdown();
        // reload history if a run just finished
        if (wasRunning && !data.running) location.reload();
      }
    }

    function pollStatus() {
      fetch('/scanner/status')
        .then(r => r.json())
        .then(applyStatus)
        .catch(() => {});
    }

    function triggerRun() {
      document.getElementById('runBtn').disabled = true;
      fetch('/scanner/run', {method: 'POST'})
        .then(r => r.json())
        .then(() => pollStatus());
    }

    updateCountdown();
    setInterval(updateCountdown, 1000);
    setInterval(pollStatus, 4000);
  </script>
</body>
</html>
"""

def _load_cities():
    import json as _json
    try:
        cfg = _json.load(open(CONFIG_FILE))
        return cfg.get("scrape", {}).get("cities") or ["amsterdam"]
    except Exception:
        return ["amsterdam"]

def _save_cities(cities: list):
    import json as _json
    try:
        with open(CONFIG_FILE) as f:
            cfg = _json.load(f)
    except Exception:
        cfg = {}
    cfg.setdefault("scrape", {})["cities"] = cities
    with open(CONFIG_FILE, "w") as f:
        _json.dump(cfg, f, indent=2)

@app.route("/")
def index():
    runs     = db.get_scan_runs(limit=100)
    cities   = _load_cities()
    interval = _load_interval()
    with _state_lock:
        state = dict(_state)
    return render_template_string(
        HISTORY_TEMPLATE,
        runs=runs,
        cities=cities,
        interval=interval,
        state=state,
        style=BASE_STYLE,
        nav=nav_html("Scanner"),
    )

@app.route("/scanner/status")
def scanner_status():
    with _state_lock:
        return jsonify(dict(_state))

@app.route("/scanner/run", methods=["POST"])
def scanner_run():
    with _state_lock:
        already_running = _state["running"]
    if not already_running:
        t = threading.Thread(target=_do_run, daemon=True)
        t.start()
        _schedule_next()
    return jsonify({"ok": True, "already_running": already_running})

@app.route("/scope", methods=["POST"])
def update_scope():
    raw    = request.form.get("cities", "")
    cities = [c.strip().lower().replace(" ", "-") for c in raw.split(",") if c.strip()]
    if cities:
        _save_cities(cities)
    return redirect(url_for("index"))

@app.route("/schedule", methods=["POST"])
def update_schedule():
    try:
        minutes = max(1, int(request.form.get("interval_minutes", 60)))
    except ValueError:
        minutes = 60
    _save_interval(minutes)
    _schedule_next(minutes * 60)
    return redirect(url_for("index"))

# ── Subscribers ───────────────────────────────────────────────────────────────

SUBSCRIBERS_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Subscribers — NL Rental Scanner</title>
  <style>
    {{ style | safe }}
    .card {
      background: #fff;
      border-radius: 12px;
      padding: 24px;
      margin-bottom: 20px;
      box-shadow: 0 1px 4px rgba(0,0,0,.07);
    }
    .card > h3 { font-size: 1rem; font-weight: 700; margin-bottom: 16px; }
    .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
    .form-group { display: flex; flex-direction: column; gap: 5px; }
    .form-group.full { grid-column: span 2; }
    label { font-size: .82rem; font-weight: 600; color: #374151; }
    input {
      padding: 9px 12px;
      border: 1px solid #d1d5db;
      border-radius: 8px;
      font-size: .9rem;
      outline: none;
      transition: border-color .15s;
    }
    input:focus { border-color: #e84e1b; }
    .btn {
      margin-top: 8px;
      padding: 10px 22px;
      background: #e84e1b;
      color: #fff;
      border: none;
      border-radius: 8px;
      font-size: .9rem;
      font-weight: 600;
      cursor: pointer;
    }
    .btn:hover { background: #c94016; }
    .btn-sm {
      padding: 6px 14px;
      background: #e84e1b;
      color: #fff;
      border: none;
      border-radius: 7px;
      font-size: .82rem;
      font-weight: 600;
      cursor: pointer;
    }
    .btn-sm:hover { background: #c94016; }

    /* Subscriber cards */
    .sub-card {
      background: #fff;
      border-radius: 12px;
      margin-bottom: 16px;
      box-shadow: 0 1px 4px rgba(0,0,0,.07);
      overflow: hidden;
    }
    .sub-header {
      display: flex;
      align-items: center;
      gap: 14px;
      padding: 16px 20px;
      border-bottom: 1px solid #f0f2f5;
    }
    .sub-avatar {
      width: 38px; height: 38px;
      border-radius: 50%;
      background: #e84e1b;
      color: #fff;
      display: flex; align-items: center; justify-content: center;
      font-weight: 700; font-size: .9rem;
      flex-shrink: 0;
    }
    .sub-info { flex: 1; }
    .sub-name { font-weight: 700; font-size: .95rem; }
    .sub-email-text { font-size: .8rem; color: #6b7280; margin-top: 1px; }
    .sub-since { font-size: .75rem; color: #9ca3af; }
    .remove-btn {
      padding: 5px 12px;
      background: transparent;
      border: 1px solid #e5e7eb;
      border-radius: 8px;
      font-size: .8rem;
      color: #9ca3af;
      cursor: pointer;
    }
    .remove-btn:hover { border-color: #e84e1b; color: #e84e1b; }

    /* Queries */
    .queries-body { padding: 14px 20px 16px; }
    .queries-label {
      font-size: .75rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .05em;
      color: #9ca3af;
      margin-bottom: 10px;
    }
    .query-row {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 10px 12px;
      background: #f9fafb;
      border-radius: 8px;
      margin-bottom: 8px;
    }
    .query-name { font-weight: 600; font-size: .88rem; min-width: 110px; }
    .query-pills { flex: 1; display: flex; flex-wrap: wrap; gap: 5px; }
    .pill {
      display: inline-block;
      padding: 2px 8px;
      background: #e5e7eb;
      border-radius: 99px;
      font-size: .75rem;
      color: #374151;
    }
    .pill-city    { background: #dbeafe; color: #1e40af; }
    .pill-student { background: #fef3c7; color: #92400e; }
    .pill-filter  { background: #ede9fe; color: #5b21b6; cursor: default; }
    .filter-text  { font-size:.82rem; color:#6b7280; padding:2px 0 4px 2px; font-style:italic; }
    .edit-query-btn { font-size:.78rem; color:#6366f1; background:none; border:none; cursor:pointer; padding:2px 0 6px; }
    .edit-query-btn:hover { text-decoration:underline; }
    .empty-queries { font-size: .83rem; color: #9ca3af; padding: 4px 0 10px; }
    .query-edit-form { background:#f9fafb; border:1px solid #e5e7eb; border-radius:8px; padding:14px 16px; margin:6px 0; display:none; }
    .query-edit-form.open { display:block; }

    /* Toast notifications */
    #toast-container { position:fixed; bottom:24px; right:24px; z-index:9999; display:flex; flex-direction:column; gap:10px; }
    .toast {
      min-width:240px; max-width:380px; padding:14px 18px; border-radius:10px;
      font-size:.9rem; font-weight:500; color:#fff; box-shadow:0 4px 16px rgba(0,0,0,.15);
      animation: toast-in .25s ease; pointer-events:none;
    }
    .toast-success { background:#16a34a; }
    .toast-error   { background:#dc2626; }
    @keyframes toast-in { from { opacity:0; transform:translateY(12px); } to { opacity:1; transform:translateY(0); } }

    /* WhatsApp group row */
    .wa-row {
      display: flex; align-items: center; gap: 10px;
      padding: 10px 20px; border-top: 1px solid #f3f4f6; margin-top: 4px;
      font-size: .85rem;
    }
    .wa-label { font-weight: 600; color: #374151; white-space: nowrap; }
    .wa-group-id {
      font-family: monospace; font-size: .82rem;
      background: #f3f4f6; padding: 3px 8px; border-radius: 6px; color: #374151;
    }
    .wa-unset { color: #9ca3af; font-style: italic; }
    .wa-edit-btn {
      font-size: .78rem; padding: 3px 10px;
      background: none; border: 1px solid #d1d5db; border-radius: 6px;
      cursor: pointer; color: #6b7280;
    }
    .wa-edit-btn:hover { border-color: #e84e1b; color: #e84e1b; }
    .wa-form { display: none; align-items: center; gap: 8px; flex: 1; }
    .wa-form.open { display: flex; }
    .wa-form input {
      flex: 1; padding: 5px 10px; border: 1px solid #d1d5db;
      border-radius: 6px; font-size: .83rem; font-family: monospace;
    }
    .wa-form input:focus { outline: none; border-color: #e84e1b; }

    /* Add query form */
    .add-query-toggle {
      font-size: .82rem;
      color: #e84e1b;
      background: none;
      border: 1px dashed #fca08c;
      border-radius: 8px;
      padding: 7px 14px;
      cursor: pointer;
      width: 100%;
      text-align: center;
      margin-top: 4px;
    }
    .add-query-toggle:hover { background: #fff5f2; }
    .add-query-form {
      display: none;
      margin-top: 12px;
      padding: 16px;
      background: #f9fafb;
      border-radius: 10px;
    }
    .add-query-form.open { display: block; }
    .city-checks { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 4px; }
    .check-item {
      display: flex; align-items: center; gap: 6px;
      font-size: .85rem; font-weight: 500; cursor: pointer;
      padding: 5px 11px;
      border: 1px solid #d1d5db; border-radius: 8px;
    }
    .check-item:has(input:checked) { border-color: #e84e1b; background: #fff5f2; color: #e84e1b; }
    .check-item input { accent-color: #e84e1b; }
    .qform-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; margin-top: 12px; }
    .qform-grid .form-group { gap: 4px; }
    .qform-grid input { padding: 7px 10px; font-size: .85rem; }
    .qform-actions { display: flex; gap: 8px; margin-top: 12px; }
    .cancel-btn {
      padding: 7px 14px;
      background: transparent;
      border: 1px solid #e5e7eb;
      border-radius: 7px;
      font-size: .82rem;
      color: #6b7280;
      cursor: pointer;
    }
    .cancel-btn:hover { border-color: #9ca3af; }

    .flash {
      background: #ecfdf5; border: 1px solid #6ee7b7; color: #065f46;
      padding: 10px 16px; border-radius: 8px; margin-bottom: 16px; font-size: .88rem;
    }
    .empty-state { text-align: center; padding: 40px; color: #9ca3af; font-size: .9rem; }
  </style>
</head>
<body>
  {{ nav | safe }}
  <div class="main">
    <h2>Subscribers</h2>

    {% if flash %}<div class="flash">{{ flash }}</div>{% endif %}

    <!-- Add subscriber form -->
    <div class="card">
      <h3>Add subscriber</h3>
      <form method="POST" action="/subscribers/add">
        <div class="form-grid">
          <div class="form-group">
            <label>First name</label>
            <input type="text" name="first_name" required placeholder="Jane">
          </div>
          <div class="form-group">
            <label>Last name</label>
            <input type="text" name="last_name" required placeholder="Smith">
          </div>
          <div class="form-group full">
            <label>Email address</label>
            <input type="email" name="email" required placeholder="jane@example.com">
          </div>
        </div>
        <button type="submit" class="btn">Add subscriber</button>
      </form>
    </div>

    <!-- Subscriber list -->
    {% if not subscribers %}
      <div class="empty-state">No subscribers yet. Add one above.</div>
    {% else %}
      {% for sub in subscribers %}
        <div class="sub-card">
          <!-- Header -->
          <div class="sub-header">
            <div class="sub-avatar">
              {{ (sub.first_name[0] if sub.first_name else sub.email[0]) | upper }}
            </div>
            <div class="sub-info">
              <div class="sub-name">{{ sub.first_name }} {{ sub.last_name }}</div>
              <div class="sub-email-text">{{ sub.email }}</div>
            </div>
            <div class="sub-since">Since {{ sub.created_at | format_time }}</div>
            <form method="POST" action="/subscribers/remove/{{ sub.id }}"
                  onsubmit="return confirm('Remove {{ sub.first_name }} {{ sub.last_name }}?')">
              <button type="submit" class="remove-btn">Remove</button>
            </form>
          </div>

          <!-- WhatsApp group -->
          <div class="wa-row">
            <span class="wa-label">WhatsApp group</span>
            <span id="wa-display-{{ sub.id }}">
              {% if sub.whatsapp_group %}
                <span class="wa-group-id">{{ sub.whatsapp_group }}</span>
              {% else %}
                <span class="wa-unset">Not configured</span>
              {% endif %}
            </span>
            <button class="wa-edit-btn" onclick="toggleWaForm({{ sub.id }})">
              {{ 'Edit' if sub.whatsapp_group else 'Set up' }}
            </button>
            <div class="wa-form" id="wa-form-{{ sub.id }}">
              <form method="POST" action="/subscribers/{{ sub.id }}/whatsapp-group"
                    style="display:flex;align-items:center;gap:8px;flex:1">
                <input type="text" name="group_id"
                       value="{{ sub.whatsapp_group }}"
                       placeholder="e.g. 120363043051405349@g.us">
                <button type="submit" class="btn-sm">Save</button>
                <button type="button" class="cancel-btn"
                        onclick="toggleWaForm({{ sub.id }})">Cancel</button>
              </form>
            </div>
          </div>

          <!-- Queries -->
          <div class="queries-body">
            <div class="queries-label">Customer queries ({{ sub.queries | length }})</div>

            {% if not sub.queries %}
              <div class="empty-queries">No queries yet — add one below.</div>
            {% else %}
              {% for q in sub.queries %}
                <div class="query-row">
                  <div class="query-name">{{ q.customer_name }}</div>
                  <div class="query-pills">
                    {% for city in q.cities %}
                      <span class="pill pill-city">{{ city | title }}</span>
                    {% endfor %}
                    {% if q.min_price or q.max_price %}
                      <span class="pill">
                        €{{ q.min_price | int if q.min_price else '?' }}–€{{ q.max_price | int if q.max_price else '?' }}/mo
                      </span>
                    {% endif %}
                    {% if q.min_rooms %}
                      <span class="pill">{{ q.min_rooms }}+ rooms</span>
                    {% endif %}
                    {% if q.student %}
                      <span class="pill pill-student">Student</span>
                    {% endif %}
                    {% if q.free_text_filter %}
                      <span class="pill pill-filter" title="{{ q.free_text_filter }}">Filter ✎</span>
                    {% endif %}
                  </div>
                  <form method="POST" action="/queries/remove/{{ q.id }}"
                        onsubmit="return confirm('Remove query for {{ q.customer_name }}?')">
                    <button type="submit" class="remove-btn">✕</button>
                  </form>
                </div>
                {% if q.free_text_filter %}
                  <div class="filter-text">{{ q.free_text_filter }}</div>
                {% endif %}
                <button class="edit-query-btn" onclick="toggleQueryEdit({{ q.id }})">✎ Edit</button>
                <div class="query-edit-form" id="query-edit-{{ q.id }}">
                  <form onsubmit="saveQueryEdit(event, {{ q.id }})">
                    <div class="form-group">
                      <label>Customer name</label>
                      <input type="text" name="customer_name" required value="{{ q.customer_name }}">
                    </div>
                    <div class="form-group" style="margin-top:10px">
                      <label>Cities</label>
                      <div class="city-checks">
                        {% for city in scope_cities %}
                          <label class="check-item">
                            <input type="checkbox" name="cities" value="{{ city }}" {{ 'checked' if city in q.cities else '' }}>
                            {{ city | title }}
                          </label>
                        {% endfor %}
                      </div>
                    </div>
                    <div class="qform-grid">
                      <div class="form-group">
                        <label>Min price (€/mo)</label>
                        <input type="number" name="min_price" placeholder="e.g. 1000" min="0" value="{{ q.min_price | int if q.min_price else '' }}">
                      </div>
                      <div class="form-group">
                        <label>Max price (€/mo)</label>
                        <input type="number" name="max_price" placeholder="e.g. 2500" min="0" value="{{ q.max_price | int if q.max_price else '' }}">
                      </div>
                      <div class="form-group">
                        <label>Min rooms</label>
                        <input type="number" name="min_rooms" placeholder="e.g. 2" min="1" max="10" value="{{ q.min_rooms | int if q.min_rooms else '' }}">
                      </div>
                    </div>
                    <div class="form-group" style="margin-top:10px">
                      <label class="check-item" style="font-weight:600">
                        <input type="checkbox" name="student" value="1" {{ 'checked' if q.student else '' }}>
                        Student? <span style="font-weight:400;color:#6b7280;font-size:.85rem">(show student-only listings)</span>
                      </label>
                    </div>
                    <div class="form-group" style="margin-top:10px">
                      <label style="font-weight:600;display:block;margin-bottom:4px">Free-text filter <span style="font-weight:400;color:#6b7280;font-size:.85rem">(optional)</span></label>
                      <textarea name="free_text_filter" rows="2" placeholder='e.g. "Exclude corner houses"'
                        style="width:100%;padding:6px 8px;border:1px solid #d1d5db;border-radius:6px;font-size:.9rem;resize:vertical;box-sizing:border-box">{{ q.free_text_filter }}</textarea>
                    </div>
                    <div class="qform-actions">
                      <button type="submit" class="btn-sm">Save changes</button>
                      <button type="button" class="cancel-btn" onclick="toggleQueryEdit({{ q.id }})">Cancel</button>
                    </div>
                  </form>
                </div>
              {% endfor %}
            {% endif %}

            <!-- Add query toggle -->
            <button class="add-query-toggle" onclick="toggleQueryForm(this)">+ Add query</button>
            <div class="add-query-form">
              <form method="POST" action="/subscribers/{{ sub.id }}/queries/add">
                <div class="form-group">
                  <label>Customer name</label>
                  <input type="text" name="customer_name" required placeholder="e.g. John">
                </div>
                <div class="form-group" style="margin-top:10px">
                  <label>Cities</label>
                  <div class="city-checks">
                    {% for city in scope_cities %}
                      <label class="check-item">
                        <input type="checkbox" name="cities" value="{{ city }}" checked>
                        {{ city | title }}
                      </label>
                    {% endfor %}
                  </div>
                </div>
                <div class="qform-grid">
                  <div class="form-group">
                    <label>Min price (€/mo)</label>
                    <input type="number" name="min_price" placeholder="e.g. 1000" min="0">
                  </div>
                  <div class="form-group">
                    <label>Max price (€/mo)</label>
                    <input type="number" name="max_price" placeholder="e.g. 2500" min="0">
                  </div>
                  <div class="form-group">
                    <label>Min rooms</label>
                    <input type="number" name="min_rooms" placeholder="e.g. 2" min="1" max="10">
                  </div>
                </div>
                <div class="form-group" style="margin-top:10px">
                  <label class="check-item" style="font-weight:600">
                    <input type="checkbox" name="student" value="1">
                    Student? <span style="font-weight:400;color:#6b7280;font-size:.85rem">(show student-only listings)</span>
                  </label>
                </div>
                <div class="form-group" style="margin-top:10px">
                  <label style="font-weight:600;display:block;margin-bottom:4px">Free-text filter <span style="font-weight:400;color:#6b7280;font-size:.85rem">(optional — describe what to exclude)</span></label>
                  <textarea name="free_text_filter" rows="2" placeholder='e.g. "Exclude corner houses" or "No properties south of A9"'
                    style="width:100%;padding:6px 8px;border:1px solid #d1d5db;border-radius:6px;font-size:.9rem;resize:vertical;box-sizing:border-box"></textarea>
                </div>
                <div class="qform-actions">
                  <button type="submit" class="btn-sm">Save query</button>
                  <button type="button" class="cancel-btn" onclick="toggleQueryForm(this.closest('.add-query-form').previousElementSibling)">Cancel</button>
                </div>
              </form>
            </div>
          </div>
        </div>
      {% endfor %}
    {% endif %}
  </div>

  <div id="toast-container"></div>
  <script>
    function toggleQueryForm(btn) {
      const form = btn.nextElementSibling;
      form.classList.toggle('open');
    }
    function toggleWaForm(subId) {
      const form    = document.getElementById('wa-form-' + subId);
      const display = document.getElementById('wa-display-' + subId);
      const isOpen  = form.classList.toggle('open');
      if (display) display.style.display = isOpen ? 'none' : '';
    }
    function toggleQueryEdit(queryId) {
      const el = document.getElementById('query-edit-' + queryId);
      el.classList.toggle('open');
    }
    function showToast(message, type) {
      const container = document.getElementById('toast-container');
      const toast = document.createElement('div');
      toast.className = 'toast toast-' + type;
      toast.textContent = message;
      container.appendChild(toast);
      setTimeout(() => toast.remove(), 5000);
    }
    async function saveQueryEdit(event, queryId) {
      event.preventDefault();
      const form = event.target;
      const data = new FormData(form);
      try {
        const resp = await fetch('/queries/' + queryId + '/edit', { method: 'POST', body: data });
        const json = await resp.json();
        if (json.ok) {
          showToast('Query saved successfully.', 'success');
          document.getElementById('query-edit-' + queryId).classList.remove('open');
          setTimeout(() => location.reload(), 1500);
        } else {
          showToast('Error: ' + (json.error || 'Could not save.'), 'error');
        }
      } catch (err) {
        showToast('Network error — could not save.', 'error');
      }
    }
  </script>
</body>
</html>
"""

@app.route("/subscribers")
def subscribers():
    subs  = db.get_subscribers_with_queries()
    flash = request.args.get("flash", "")
    return render_template_string(
        SUBSCRIBERS_TEMPLATE,
        subscribers=subs,
        scope_cities=_load_cities(),
        flash=flash,
        style=BASE_STYLE,
        nav=nav_html("Subscribers"),
    )

@app.route("/subscribers/add", methods=["POST"])
def add_subscriber():
    email      = request.form.get("email", "").strip()
    first_name = request.form.get("first_name", "").strip()
    last_name  = request.form.get("last_name", "").strip()
    db.add_subscriber(email, first_name, last_name)
    return redirect(url_for("subscribers", flash=f"{first_name} {last_name} added."))

@app.route("/subscribers/<int:sub_id>/queries/add", methods=["POST"])
def add_query(sub_id):
    customer_name     = request.form.get("customer_name", "").strip()
    cities            = request.form.getlist("cities") or _load_cities()
    min_price         = request.form.get("min_price") or None
    max_price         = request.form.get("max_price") or None
    min_rooms         = request.form.get("min_rooms") or None
    student           = request.form.get("student") == "1"
    free_text_filter  = request.form.get("free_text_filter", "").strip()
    db.add_customer_query(
        sub_id, customer_name, cities,
        float(min_price) if min_price else None,
        float(max_price) if max_price else None,
        int(min_rooms)   if min_rooms else None,
        student,
        free_text_filter,
    )
    return redirect(url_for("subscribers", flash=f"Query for '{customer_name}' added."))

@app.route("/subscribers/remove/<int:sub_id>", methods=["POST"])
def remove_subscriber(sub_id):
    db.remove_subscriber(sub_id)
    return redirect(url_for("subscribers", flash="Subscriber removed."))

@app.route("/queries/remove/<int:query_id>", methods=["POST"])
def remove_query(query_id):
    db.remove_customer_query(query_id)
    return redirect(url_for("subscribers", flash="Query removed."))

@app.route("/subscribers/<int:sub_id>/whatsapp-group", methods=["POST"])
def set_whatsapp_group(sub_id):
    group_id = request.form.get("group_id", "").strip()
    db.set_subscriber_whatsapp_group(sub_id, group_id)
    msg = f"WhatsApp group {'updated' if group_id else 'cleared'}."
    return redirect(url_for("subscribers", flash=msg))

@app.route("/whatsapp-groups")
def list_whatsapp_groups():
    """
    Proxy to whatsapp-service GET /groups.
    Returns the groups the linked WhatsApp account belongs to so you can
    copy the chatId values into subscriber settings.
    """
    import json as _json
    try:
        cfg = _json.load(open(CONFIG_FILE))
    except Exception:
        cfg = {}
    ntfy        = cfg.get("notifications", {})
    service_url = ntfy.get("whatsapp_service_url", "http://localhost:3001").rstrip("/")
    token       = ntfy.get("whatsapp_service_token", "").strip()
    if not service_url:
        return jsonify({"error": "whatsapp_service_url not set in config.json"}), 400
    import requests as _req
    try:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        r = _req.get(f"{service_url}/groups", headers=headers, timeout=10)
        if r.status_code == 200:
            return jsonify(r.json())
        return jsonify({"error": f"whatsapp-service returned {r.status_code}: {r.text[:200]}"}), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/queries/<int:query_id>/filter", methods=["POST"])
def update_query_filter(query_id):
    """Update the free-text filter for a customer query from the UI."""
    free_text_filter = request.form.get("free_text_filter", "").strip()
    db.update_query_filter(query_id, free_text_filter)
    msg = "Filter updated." if free_text_filter else "Filter cleared."
    return redirect(url_for("subscribers", flash=msg))


@app.route("/queries/<int:query_id>/edit", methods=["POST"])
def edit_query(query_id):
    """AJAX endpoint to update all fields of a customer query. Returns JSON."""
    customer_name    = request.form.get("customer_name", "").strip()
    cities           = request.form.getlist("cities") or _load_cities()
    min_price        = request.form.get("min_price") or None
    max_price        = request.form.get("max_price") or None
    min_rooms        = request.form.get("min_rooms") or None
    student          = request.form.get("student") == "1"
    free_text_filter = request.form.get("free_text_filter", "").strip()
    if not customer_name:
        return jsonify({"error": "customer_name is required"}), 400
    db.update_customer_query(
        query_id, customer_name, cities,
        float(min_price) if min_price else None,
        float(max_price) if max_price else None,
        int(min_rooms)   if min_rooms else None,
        student,
        free_text_filter,
    )
    return jsonify({"ok": True})


@app.route("/api/whatsapp-filter", methods=["POST"])
def whatsapp_filter_webhook():
    """
    Called by the WhatsApp microservice when a user replies to a listing
    notification with 'add filter: <instruction>'.

    Expected JSON body:
      {
        "group_id":       "120363407400776027@g.us",
        "quoted_message": "<text of the bot message that was replied to>",
        "filter_text":    "<everything the user said after 'add filter'>",
      }

    Returns:
      {
        "acknowledgements": [
          {"customer_name": "...", "query_id": 1, "new_filter": "..."},
          ...
        ]
      }
    """
    import json as _json, re as _re
    data = request.get_json(force=True) or {}
    group_id       = data.get("group_id", "").strip()
    quoted_message = data.get("quoted_message", "")
    filter_text    = data.get("filter_text", "").strip()

    if not filter_text:
        return jsonify({"error": "filter_text is required"}), 400

    # Parse customer names from the bot message (format: "— *CustomerName*")
    customer_names_found = _re.findall(r"—\s+\*(.+?)\*", quoted_message)
    if not customer_names_found:
        return jsonify({"error": "Could not identify any customer from the quoted message"}), 400

    # Find matching queries across subscribers in this group
    subscribers = db.get_subscribers_with_queries()
    targets = []
    for sub in subscribers:
        if group_id and sub.get("whatsapp_group", "") != group_id:
            continue
        for q in sub.get("queries", []):
            if q.get("customer_name", "").lower() in [n.lower() for n in customer_names_found]:
                targets.append(q)

    if not targets:
        return jsonify({"error": "No matching customer queries found for this group"}), 404

    # Load API key for LLM filter merging
    try:
        cfg = _json.load(open(CONFIG_FILE))
    except Exception:
        cfg = {}
    api_key = cfg.get("openai_api_key", "").strip()

    from scanner import merge_free_text_filter
    acknowledgements = []
    for q in targets:
        existing = q.get("free_text_filter", "")
        new_filter = merge_free_text_filter(existing, filter_text, api_key)
        db.update_query_filter(q["id"], new_filter)
        acknowledgements.append({
            "customer_name": q["customer_name"],
            "query_id":      q["id"],
            "new_filter":    new_filter,
        })

    return jsonify({"acknowledgements": acknowledgements})


@app.route("/api/health")
def api_health():
    import json as _json
    import urllib.request

    with _state_lock:
        scheduler = {
            "running": _state["running"],
            "next_run_at": _state["next_run_at"],
            "started_at": _state["started_at"],
        }

    # last scan from DB
    runs = db.get_scan_runs(limit=1)
    last_scan = runs[0]["ran_at"] if runs else None

    # whatsapp-service health (best-effort, 2 s timeout)
    wa_status = {"reachable": False}
    try:
        cfg = _json.load(open(CONFIG_FILE))
        wa_url = cfg.get("notifications", {}).get(
            "whatsapp_service_url", "http://localhost:3001"
        )
        token = cfg.get("notifications", {}).get("whatsapp_service_token", "")
        req = urllib.request.Request(
            f"{wa_url}/health",
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            wa_status = _json.loads(r.read())
            wa_status["reachable"] = True
    except Exception:
        pass

    return jsonify({
        "ok": True,
        "scheduler": scheduler,
        "last_scan_at": last_scan,
        "whatsapp": wa_status,
    })


if __name__ == "__main__":
    print("Scanner UI running at http://localhost:5001")
    start_scheduler()
    app.run(port=5001, debug=False, use_reloader=False)
