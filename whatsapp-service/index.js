/**
 * whatsapp-service — HTTP microservice for sending WhatsApp messages.
 *
 * Uses Baileys (open-source WhatsApp Web protocol implementation) so messages
 * travel directly from this machine to WhatsApp's servers — no third-party
 * intermediary, no data leakage.
 *
 * Endpoints
 * ---------
 *   GET  /health          Connection status
 *   GET  /qr              QR code HTML page (scan to pair on first run)
 *   POST /send            Send a message to a chat or group
 *   GET  /groups          List all WhatsApp group chats
 *
 * Environment variables
 * ----------------------
 *   PORT        HTTP port to listen on          (default: 3001)
 *   API_TOKEN   Bearer token for auth           (default: "" = no auth)
 *
 * Auth
 * ----
 *   If API_TOKEN is set every request must include:
 *     Authorization: Bearer <token>
 *
 * Session persistence
 * -------------------
 *   Credentials are saved in ./auth_info_baileys/ after the first QR scan.
 *   Keep that directory — deleting it forces a new QR scan.
 *
 * First-run flow
 * --------------
 *   1. npm start
 *   2. Open http://localhost:3001/qr in a browser and scan with WhatsApp
 *   3. The service reconnects automatically from then on
 */

"use strict";

const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
} = require("@whiskeysockets/baileys");

const express = require("express");
const pino = require("pino");
const qrcode = require("qrcode");
const qrcodeTerminal = require("qrcode-terminal");
const path = require("path");

// ── Config ────────────────────────────────────────────────────────────────────

const PORT = parseInt(process.env.PORT || "3001", 10);
const API_TOKEN = (process.env.API_TOKEN || "").trim();
const AUTH_DIR = path.join(__dirname, "auth_info_baileys");

// ── State ─────────────────────────────────────────────────────────────────────

let sock = null;
let latestQR = null;      // raw QR string; null once connected
let isConnected = false;
let isReconnecting = false;

const logger = pino({ level: "silent" }); // suppress Baileys' verbose internal logs

// ── WhatsApp connection ───────────────────────────────────────────────────────

async function connect() {
  if (isReconnecting) return;
  isReconnecting = true;

  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();

  sock = makeWASocket({
    version,
    auth: {
      creds: state.creds,
      keys: makeCacheableSignalKeyStore(state.keys, logger),
    },
    logger,
    printQRInTerminal: false, // we handle QR ourselves
    browser: ["WhatsApp Service", "Chrome", "1.0.0"],
    markOnlineOnConnect: false,
    syncFullHistory: false,
  });

  sock.ev.on("connection.update", (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      latestQR = qr;
      isConnected = false;
      qrcodeTerminal.generate(qr, { small: true });
      console.log("[WA] QR code ready — open http://localhost:" + PORT + "/qr to scan.");
    }

    if (connection === "open") {
      isConnected = true;
      latestQR = null;
      isReconnecting = false;
      console.log("[WA] Connected.");
    }

    if (connection === "close") {
      isConnected = false;
      isReconnecting = false;

      const statusCode = lastDisconnect?.error?.output?.statusCode;
      const loggedOut = statusCode === DisconnectReason.loggedOut;

      if (loggedOut) {
        console.log("[WA] Logged out. Delete auth_info_baileys/ and restart to pair again.");
      } else {
        const delay = 3000;
        console.log(`[WA] Disconnected (${statusCode}). Reconnecting in ${delay / 1000}s…`);
        setTimeout(connect, delay);
      }
    }
  });

  sock.ev.on("creds.update", saveCreds);
}

// ── HTTP server ───────────────────────────────────────────────────────────────

const app = express();
app.use(express.json());

/** Bearer-token middleware — skipped if API_TOKEN is empty. */
function auth(req, res, next) {
  if (!API_TOKEN) return next();
  const header = req.headers["authorization"] || "";
  if (header === "Bearer " + API_TOKEN) return next();
  res.status(401).json({ error: "Unauthorized" });
}

// ── GET /health ───────────────────────────────────────────────────────────────

app.get("/health", auth, (req, res) => {
  res.json({
    connected: isConnected,
    waiting_for_qr: latestQR !== null,
  });
});

// ── GET /qr ───────────────────────────────────────────────────────────────────

app.get("/qr", auth, async (req, res) => {
  if (isConnected) {
    return res.send("<h2>Already connected ✓</h2><p>No QR needed.</p>");
  }
  if (!latestQR) {
    return res.send(
      "<h2>Waiting for QR…</h2><p>Refresh in a few seconds.</p>" +
        '<script>setTimeout(()=>location.reload(),3000)</script>'
    );
  }
  try {
    const dataUrl = await qrcode.toDataURL(latestQR, { width: 300, margin: 2 });
    res.send(
      `<!DOCTYPE html><html><head><title>WhatsApp QR</title></head><body style="font-family:sans-serif;text-align:center;padding:40px">` +
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
// Body: { "chatId": "120363043051405349@g.us", "message": "Hello!" }
//
// chatId formats:
//   Group : "120363043051405349@g.us"
//   Person: "31612345678@s.whatsapp.net"

app.post("/send", auth, async (req, res) => {
  if (!isConnected) {
    return res.status(503).json({ error: "WhatsApp not connected. Scan QR at /qr first." });
  }

  const { chatId, message } = req.body || {};
  if (!chatId || typeof chatId !== "string") {
    return res.status(400).json({ error: "chatId is required (e.g. 120363043051405349@g.us)" });
  }
  if (!message || typeof message !== "string") {
    return res.status(400).json({ error: "message is required" });
  }

  try {
    await sock.sendMessage(chatId, { text: message });
    console.log(`[WA] Sent to ${chatId}.`);
    res.json({ ok: true });
  } catch (err) {
    console.error("[WA] Send error:", err.message);
    res.status(500).json({ error: err.message });
  }
});

// ── GET /groups ───────────────────────────────────────────────────────────────

app.get("/groups", auth, async (req, res) => {
  if (!isConnected) {
    return res.status(503).json({ error: "WhatsApp not connected. Scan QR at /qr first." });
  }

  try {
    const chats = await sock.groupFetchAllParticipating();
    const groups = Object.values(chats).map((g) => ({
      id: g.id,
      name: g.subject,
      participants: g.participants?.length ?? 0,
    }));
    groups.sort((a, b) => a.name.localeCompare(b.name));
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
  connect().catch((err) => console.error("[WA] Connect error:", err));
});
