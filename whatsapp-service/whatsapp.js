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
/** Optional async (msg, client, quotedMsg) => void — set via setReplyHandler() */
let _replyHandler = null;
/** IDs of messages sent by this bot session — excluded from message_create processing. */
const _sentByBot = new Set();

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
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
  ];
  const opts = { headless: true, args };
  if (process.env.CHROME_PATH) {
    opts.executablePath = process.env.CHROME_PATH;
  }
  return opts;
}

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {
  // Destroy previous client (and its Chrome process) before creating a fresh one.
  if (client) {
    try { await client.destroy(); } catch (_) {}
    client = null;
  }

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

  /** Shared handler for both incoming (message) and self-sent (message_create) messages. */
  async function handleMessage(msg) {
    console.log(`[WA] Incoming message from ${msg.from}: type=${msg.type} hasQuote=${msg.hasQuotedMsg} fromMe=${msg.fromMe}`);
    // Replies to bot messages are handled by the listing-reply handler (if set)
    // before reaching the command dispatcher — this is how filter updates work.
    if (_replyHandler && msg.hasQuotedMsg) {
      try {
        const quoted = await msg.getQuotedMessage();
        if (quoted.fromMe) {
          await _replyHandler(msg, client, quoted);
          return;
        }
      } catch (err) {
        console.error("[WA] Reply handler error:", err.message);
      }
    }
    await commands.dispatch(msg, client);
  }

  // "message" fires for messages from other accounts.
  client.on("message", handleMessage);

  // "message_create" fires for all messages including your own phone's replies
  // (when the bot is a linked device on the subscriber's own WhatsApp account).
  // Skip messages originated by this web session: WhatsApp Web assigns IDs starting
  // with "3EB" to messages sent from the browser; phone-originated messages have
  // a different format so we can reliably tell them apart.
  client.on("message_create", async (msg) => {
    if (!msg.fromMe) return;                          // already handled by "message"
    const id = msg.id && msg.id.id;
    if (id && id.startsWith("3EB")) return;           // sent by this web session, skip
    await handleMessage(msg);
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

/** True when the error is a stale Puppeteer frame (WhatsApp Web reloaded). */
function isFrameDetached(err) {
  return err && typeof err.message === "string" && err.message.includes("detached Frame");
}

/** Mark not-ready and schedule a re-init if the page frame was detached. */
function handleFrameDetached() {
  console.log("[WA] Detached frame detected — WhatsApp Web reloaded. Reinitialising in 3 s…");
  _isReady = false;
  setTimeout(() => init(), 3000);
}

/**
 * Send a text message to a chat or group.
 * @param {string} chatId  e.g. "120363407400776027@g.us" or "31612345678@c.us"
 * @param {string} text    Message body (WhatsApp markdown: *bold*, _italic_)
 */
async function send(chatId, text) {
  if (!_isReady) throw new Error("WhatsApp not ready");
  try {
    await client.sendMessage(chatId, text);
  } catch (err) {
    if (isFrameDetached(err)) { handleFrameDetached(); }
    throw err;
  }
}

/**
 * List all group chats the linked account belongs to.
 * @returns {Promise<Array<{id: string, name: string, participants: number}>>}
 */
async function getGroups() {
  if (!_isReady) throw new Error("WhatsApp not ready");
  try {
    const chats = await client.getChats();
    return chats
      .filter((c) => c.isGroup)
      .map((c) => ({ id: c.id._serialized, name: c.name, participants: c.participants?.length ?? 0 }))
      .sort((a, b) => a.name.localeCompare(b.name));
  } catch (err) {
    if (isFrameDetached(err)) { handleFrameDetached(); }
    throw err;
  }
}

/**
 * Register a handler called whenever someone replies to a message sent by the
 * bot. Runs before the command dispatcher so it takes priority.
 *
 * @param {(msg: Message, client: Client, quotedMsg: Message) => Promise<void>} handler
 */
function setReplyHandler(handler) {
  _replyHandler = handler;
}

module.exports = { init, isReady, getQR, send, getGroups, setReplyHandler };
