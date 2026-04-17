/**
 * whatsapp.js — WhatsApp client singleton.
 *
 * Wraps whatsapp-web.js in a single stateful module so the rest of the
 * service never touches the WA client directly.  All incoming messages are
 * routed through the command dispatcher in commands.js.
 *
 * Public API
 * ----------
 *   init()          Connect (or restore session) — call once at startup
 *   isReady()       true when the client is authenticated and ready to send
 *   getQR()         latest QR string, or null when already connected
 *   send(chatId, text)   Send a text message to any chat / group
 *   getGroups()     Resolve to [{id, name, participants}] list of groups
 */

"use strict";

const { Client, LocalAuth } = require("whatsapp-web.js");
const qrcodeTerminal = require("qrcode-terminal");
const commands = require("./commands");
const path = require("path");

// ── State ─────────────────────────────────────────────────────────────────────

let client = null;
let _isReady = false;
let _latestQR = null;

// ── Puppeteer / Chrome config ─────────────────────────────────────────────────

/**
 * Build the puppeteer launch options.
 * Set CHROME_PATH env var to use your own Chrome/Chromium binary instead of
 * the one bundled with puppeteer (saves disk space, avoids download).
 *
 * Example:
 *   CHROME_PATH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" npm start
 */
function puppeteerArgs() {
  const args = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-accelerated-2d-canvas",
    "--no-first-run",
    "--disable-gpu",
  ];
  const opts = { headless: true, args };
  if (process.env.CHROME_PATH) {
    opts.executablePath = process.env.CHROME_PATH;
  }
  return opts;
}

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {
  client = new Client({
    authStrategy: new LocalAuth({
      dataPath: path.join(__dirname, "auth_data"),
    }),
    puppeteer: puppeteerArgs(),
    // Increase timeout for slow machines
    authTimeoutMs: 60_000,
    webVersionCache: {
      type: "remote",
      remotePath: "https://raw.githubusercontent.com/wppconnect-team/wa-version/main/html/2.3000.1023170293.html",
    },
  });

  // ── Events ────────────────────────────────────────────────────────────────

  client.on("qr", (qr) => {
    _latestQR = qr;
    _isReady = false;
    qrcodeTerminal.generate(qr, { small: true });
    console.log("[WA] QR code ready — open http://localhost:" + (process.env.PORT || 3001) + "/qr to scan.");
  });

  client.on("authenticated", () => {
    _latestQR = null;
    console.log("[WA] Authenticated.");
  });

  client.on("ready", () => {
    _isReady = true;
    _latestQR = null;
    console.log("[WA] Connected and ready.");
  });

  client.on("auth_failure", (msg) => {
    console.error("[WA] Auth failure:", msg);
    _isReady = false;
  });

  client.on("disconnected", (reason) => {
    console.log("[WA] Disconnected:", reason, "— reconnecting in 5 s…");
    _isReady = false;
    setTimeout(() => init(), 5000);
  });

  // Route every incoming message through the command dispatcher.
  // To handle a new command, call commands.register() in index.js (or any
  // module loaded before init() is called).
  client.on("message", async (msg) => {
    await commands.dispatch(msg, client);
  });

  await client.initialize();
}

// ── Public API ────────────────────────────────────────────────────────────────

function isReady() {
  return _isReady;
}

function getQR() {
  return _latestQR;
}

/**
 * Send a text message to a chat or group.
 * @param {string} chatId  e.g. "120363407400776027@g.us" or "31612345678@c.us"
 * @param {string} text    Message body (WhatsApp markdown: *bold*, _italic_)
 */
async function send(chatId, text) {
  if (!_isReady) throw new Error("WhatsApp not ready");
  await client.sendMessage(chatId, text);
}

/**
 * List all group chats the linked account belongs to.
 * @returns {Promise<Array<{id: string, name: string, participants: number}>>}
 */
async function getGroups() {
  if (!_isReady) throw new Error("WhatsApp not ready");
  const chats = await client.getChats();
  return chats
    .filter((c) => c.isGroup)
    .map((c) => ({ id: c.id._serialized, name: c.name, participants: c.participants?.length ?? 0 }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

module.exports = { init, isReady, getQR, send, getGroups };
