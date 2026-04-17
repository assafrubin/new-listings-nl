/**
 * whatsapp-service — HTTP microservice for sending WhatsApp messages.
 *
 * Uses whatsapp-web.js (Puppeteer + real Chrome running WhatsApp Web) so
 * messages travel directly from this machine to WhatsApp's servers with no
 * third-party intermediary.
 *
 * Endpoints
 * ---------
 *   GET  /health    Connection status
 *   GET  /qr        QR code page — scan on first run to pair your number
 *   POST /send      Send a message to a group or individual chat
 *   GET  /groups    List all WhatsApp group chats
 *
 * Environment variables
 * ----------------------
 *   PORT        HTTP port (default: 3001)
 *   API_TOKEN   Bearer token for auth — leave empty to disable auth
 *   CHROME_PATH Path to Chrome/Chromium binary (optional; uses bundled if unset)
 *
 * Adding new WhatsApp commands
 * ----------------------------
 * Register them in this file (or in a separate module imported here) before
 * wa.init() is called.  See commands.js for the full API.
 *
 *   const commands = require('./commands')
 *   commands.register('!search', async (msg, client, args) => {
 *     await msg.reply(`Searching for: ${args}`)
 *   }, 'Search listings')
 */

"use strict";

const express = require("express");
const qrcode = require("qrcode");
const wa = require("./whatsapp");
// const commands = require('./commands')  // uncomment to add commands here

// ── Config ────────────────────────────────────────────────────────────────────

const PORT = parseInt(process.env.PORT || "3001", 10);
const API_TOKEN = (process.env.API_TOKEN || "").trim();

// ── HTTP server ───────────────────────────────────────────────────────────────

const app = express();
app.use(express.json());

/** Bearer-token auth — skipped entirely when API_TOKEN is empty. */
function auth(req, res, next) {
  if (!API_TOKEN) return next();
  if (req.headers["authorization"] === "Bearer " + API_TOKEN) return next();
  res.status(401).json({ error: "Unauthorized" });
}

// ── GET /health ───────────────────────────────────────────────────────────────

app.get("/health", auth, (req, res) => {
  res.json({ connected: wa.isReady(), waiting_for_qr: wa.getQR() !== null });
});

// ── GET /qr ───────────────────────────────────────────────────────────────────

app.get("/qr", auth, async (req, res) => {
  if (wa.isReady()) {
    return res.send("<h2>Already connected ✓</h2><p>No QR needed.</p>");
  }
  const qr = wa.getQR();
  if (!qr) {
    return res.send(
      "<h2>Waiting for QR…</h2><p>Refresh in a few seconds.</p>" +
        "<script>setTimeout(()=>location.reload(),3000)</script>"
    );
  }
  try {
    const dataUrl = await qrcode.toDataURL(qr, { width: 300, margin: 2 });
    res.send(
      `<!DOCTYPE html><html><head><title>WhatsApp QR</title></head>` +
        `<body style="font-family:sans-serif;text-align:center;padding:40px">` +
        `<h2>Scan with WhatsApp</h2>` +
        `<p>Open WhatsApp → Linked Devices → Link a Device</p>` +
        `<img src="${dataUrl}" style="border:1px solid #ccc;border-radius:8px"/>` +
        `<p style="color:#666;font-size:.9em">Page auto-refreshes every 20 s</p>` +
        `<script>setTimeout(()=>location.reload(),20000)</script>` +
        `</body></html>`
    );
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ── POST /send ────────────────────────────────────────────────────────────────
//
// Body: { "chatId": "120363407400776027@g.us", "message": "Hello!" }

app.post("/send", auth, async (req, res) => {
  if (!wa.isReady()) {
    return res.status(503).json({ error: "WhatsApp not connected. Scan QR at /qr first." });
  }
  const { chatId, message } = req.body || {};
  if (!chatId || typeof chatId !== "string") {
    return res.status(400).json({ error: "chatId is required (e.g. 120363407400776027@g.us)" });
  }
  if (!message || typeof message !== "string") {
    return res.status(400).json({ error: "message is required" });
  }
  try {
    await wa.send(chatId, message);
    console.log(`[WA] Sent to ${chatId}.`);
    res.json({ ok: true });
  } catch (err) {
    console.error("[WA] Send error:", err.message);
    res.status(500).json({ error: err.message });
  }
});

// ── GET /groups ───────────────────────────────────────────────────────────────

app.get("/groups", auth, async (req, res) => {
  if (!wa.isReady()) {
    return res.status(503).json({ error: "WhatsApp not connected. Scan QR at /qr first." });
  }
  try {
    const groups = await wa.getGroups();
    res.json({ groups });
  } catch (err) {
    console.error("[WA] Groups error:", err.message);
    res.status(500).json({ error: err.message });
  }
});

// ── Start ─────────────────────────────────────────────────────────────────────

app.listen(PORT, () => {
  console.log(`[WA] HTTP service listening on http://localhost:${PORT}`);
  if (API_TOKEN) {
    console.log("[WA] Auth enabled — set Authorization: Bearer <token> on all requests.");
  } else {
    console.log("[WA] Auth disabled — set API_TOKEN env var to enable.");
  }
  wa.init().catch((err) => console.error("[WA] Init error:", err));
});
