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
 *   SCANNER_URL Base URL of the Python scanner app (default: http://localhost:5001)
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
const https = require("https");
const http = require("http");
const fs = require("fs");
const os = require("os");
const path = require("path");
const wa = require("./whatsapp");
// commands.js is still available for !ping / !help and future additions
require("./commands");

// ── Config ────────────────────────────────────────────────────────────────────

const PORT = parseInt(process.env.PORT || "3001", 10);
const API_TOKEN = (process.env.API_TOKEN || "").trim();
const SCANNER_URL = (process.env.SCANNER_URL || "http://127.0.0.1:5001").replace(/\/$/, "");

// OpenAI key — env var takes priority, falls back to config.json
function loadOpenAiKey() {
  if (process.env.OPENAI_API_KEY) return process.env.OPENAI_API_KEY.trim();
  try {
    const cfg = JSON.parse(fs.readFileSync(path.join(__dirname, "../config.json"), "utf8"));
    return (cfg.openai_api_key || "").trim();
  } catch (_) { return ""; }
}
const OPENAI_API_KEY = loadOpenAiKey();

// ── Voice transcription (OpenAI Whisper) ─────────────────────────────────────

// Whisper prompt: comma-separated proper nouns across all 5 cities.
// Whisper uses this as a vocabulary prior — the more specific proper nouns
// listed here, the less likely it is to invent phonetically similar nonsense.
const WHISPER_PROMPT =
  // Amsterdam — neighborhoods, parks, streets
  "Vondelpark, Jordaan, De Pijp, Oud-Zuid, Oud-West, Oud-Noord, Centrum, " +
  "IJburg, Zeeburg, Buitenveldert, Rivierenbuurt, Zuidas, Slotervaart, " +
  "Geuzenveld, Indische Buurt, Dapperbuurt, Plantage, Watergraafsmeer, " +
  "Museumplein, Leidseplein, Rembrandtplein, Frederiksplein, Sarphatipark, " +
  "Beatrixpark, Westerpark, Oosterpark, Amstelpark, Amstel, " +
  "Haarlemmerdijk, Utrechtsestraat, Ceintuurbaan, Overtoom, Kinkerstraat, " +
  "A10, S100, S108, S111, " +
  // Amstelveen — neighborhoods and key road
  "Amstelveen, Stadshart, Middenhoven, Westwijk, Legmeer, Bankras, Kostverloren, " +
  "Amstelveenseweg, A9, " +
  // Utrecht — neighborhoods, parks, roads
  "Utrecht, Binnenstad, Lombok, Kanaleneiland, Leidsche Rijn, Overvecht, " +
  "Houten, Zeist, Griftpark, Wilhelminapark, Amelisweerd, " +
  "Wittevrouwensingel, Catharijnesingel, Maliesingel, Vredenburg, A2, A12, A27, " +
  // Rotterdam — neighborhoods, parks, roads
  "Rotterdam, Kralingen, Hillegersberg, Delfshaven, Feijenoord, IJsselmonde, " +
  "Schiedam, Capelle aan den IJssel, Kralingse Bos, Vroesenpark, Zuiderpark, " +
  "Erasmusbrug, Maastunnel, A20, A16, " +
  // Den Haag — neighborhoods, parks, roads
  "Den Haag, Scheveningen, Statenkwartier, Benoordenhout, Bezuidenhout, " +
  "Segbroek, Escamp, Haagse Hout, Loosduinen, Leidschenveen, Ypenburg, " +
  "Haagse Bos, Westbroekpark, Clingendael, " +
  "Laan van Meerdervoort, Fahrenheitstraat, Leyweg, A4, A13. " +
  // Common filter vocabulary
  "Directions: north, south, east, west, noordkant, zuidkant, ten noorden, ten zuiden.";

/**
 * Transcribe a WhatsApp voice message (ptt / audio) to text, then run a GPT
 * cleanup pass to fix proper nouns and semantic errors (e.g. north↔south).
 * Returns the corrected transcript string, or null if transcription failed.
 */
async function transcribeVoice(msg) {
  if (!OPENAI_API_KEY) return null;

  const media = await msg.downloadMedia();
  if (!media || !media.data) return null;

  const ext = media.mimetype.includes("ogg") ? "ogg" : "mp4";
  const tmpFile = path.join(os.tmpdir(), `wa_audio_${Date.now()}.${ext}`);
  fs.writeFileSync(tmpFile, Buffer.from(media.data, "base64"));

  let raw = null;
  try {
    const FormData = require("form-data");
    const form = new FormData();
    form.append("file", fs.createReadStream(tmpFile), { filename: `audio.${ext}` });
    form.append("model", "whisper-1");
    form.append("language", "he");
    form.append("prompt", WHISPER_PROMPT);

    const resp = await new Promise((resolve, reject) => {
      const req = https.request(
        {
          hostname: "api.openai.com",
          path: "/v1/audio/transcriptions",
          method: "POST",
          headers: {
            Authorization: `Bearer ${OPENAI_API_KEY}`,
            ...form.getHeaders(),
          },
        },
        (res) => {
          let data = "";
          res.on("data", (c) => (data += c));
          res.on("end", () => resolve(JSON.parse(data)));
        }
      );
      req.on("error", reject);
      form.pipe(req);
    });

    raw = resp.text || null;
  } finally {
    try { fs.unlinkSync(tmpFile); } catch (_) {}
  }

  if (!raw) return null;
  console.log(`[WA] Whisper raw: "${raw}"`);

  // GPT cleanup: fix proper nouns and semantic errors in the Amsterdam rental context
  const cleaned = await correctTranscript(raw);
  console.log(`[WA] GPT corrected: "${cleaned}"`);
  return cleaned;
}

/**
 * Ask GPT to correct a Whisper transcript in the context of Amsterdam rental
 * search filters — fixes misspelled place names, flipped directions, etc.
 */
async function correctTranscript(transcript) {
  try {
    const payload = JSON.stringify({
      model: "gpt-4o-mini",
      max_tokens: 200,
      messages: [
        {
          role: "system",
          content:
            "You correct speech-to-text transcripts for a Dutch rental search app. " +
            "The user speaks Hebrew but mentions Dutch place names, streets, and roads. " +
            "Fix misheard proper nouns using the geographic reference below. " +
            "Correct obvious directional errors. Return only the corrected English text — no explanation.\n\n" +
            "GEOGRAPHIC REFERENCE:\n" +
            "Amsterdam: neighborhoods — Jordaan, De Pijp, Oud-Zuid, Oud-West, Centrum, IJburg, Buitenveldert, Rivierenbuurt, Zuidas, Indische Buurt, Dapperbuurt, Plantage, Watergraafsmeer, Slotervaart, Geuzenveld. " +
            "Parks — Vondelpark, Beatrixpark, Westerpark, Oosterpark, Sarphatipark, Amstelpark. " +
            "Roads/squares — A10 (ring road), Museumplein, Leidseplein, Rembrandtplein, Overtoom, Ceintuurbaan, Utrechtsestraat, Haarlemmerdijk, Kinkerstraat.\n" +
            "Amstelveen: neighborhoods — Middenhoven, Westwijk, Legmeer, Bankras, Stadshart. " +
            "Key road — A9 (major highway running east-west through Amstelveen; 'south of A9' means the southern part of Amstelveen). " +
            "Road — Amstelveenseweg (connects Amsterdam to Amstelveen).\n" +
            "Utrecht: neighborhoods — Lombok, Kanaleneiland, Leidsche Rijn, Overvecht, Binnenstad. " +
            "Parks — Wilhelminapark, Griftpark, Amelisweerd. Roads — A2, A12, A27, Catharijnesingel, Maliesingel.\n" +
            "Rotterdam: neighborhoods — Kralingen, Hillegersberg, Delfshaven, Feijenoord. " +
            "Parks — Kralingse Bos, Vroesenpark. Roads — A20, A16, Erasmusbrug.\n" +
            "Den Haag: neighborhoods — Scheveningen, Statenkwartier, Benoordenhout, Bezuidenhout, Segbroek, Haagse Hout, Loosduinen. " +
            "Parks — Haagse Bos, Westbroekpark, Clingendael. Roads — A4, A13, Laan van Meerdervoort.",
        },
        { role: "user", content: transcript },
      ],
    });

    const result = await new Promise((resolve, reject) => {
      const req = https.request(
        {
          hostname: "api.openai.com",
          path: "/v1/chat/completions",
          method: "POST",
          headers: {
            Authorization: `Bearer ${OPENAI_API_KEY}`,
            "Content-Type": "application/json",
            "Content-Length": Buffer.byteLength(payload),
          },
        },
        (res) => {
          let data = "";
          res.on("data", (c) => (data += c));
          res.on("end", () => resolve(JSON.parse(data)));
        }
      );
      req.on("error", reject);
      req.write(payload);
      req.end();
    });

    return result.choices?.[0]?.message?.content?.trim() || transcript;
  } catch (err) {
    console.error("[WA] Transcript correction error:", err.message);
    return transcript;
  }
}

// ── Listing-reply handler ─────────────────────────────────────────────────────
//
// Any reply to a message the bot sent is treated as a filter instruction for
// the customer queries shown in that listing message.
//
// Supported input types:
//   • Text  — sent directly to the LLM (English or Hebrew)
//   • Voice — transcribed via OpenAI Whisper, then sent to the LLM

wa.setReplyHandler(async (msg, client, quoted) => {
  const isVoice = msg.type === "ptt" || msg.type === "audio";
  let filterText = "";

  if (isVoice) {
    console.log(`[WA] Voice reply detected. OPENAI_API_KEY set: ${!!OPENAI_API_KEY}`);
    if (!OPENAI_API_KEY) {
      await msg.reply(
        "I received your voice message but voice transcription is not configured.\n" +
        "Please type your feedback as a text reply to the listing instead."
      );
      return;
    }
    try {
      filterText = await transcribeVoice(msg);
      console.log(`[WA] Transcription result: "${filterText}"`);
      if (!filterText) {
        await msg.reply("I couldn't transcribe your voice message. Please try again or type your feedback.");
        return;
      }
    } catch (err) {
      console.error("[WA] Transcription error:", err.message);
      await msg.reply("Transcription failed. Please type your feedback instead.");
      return;
    }
  } else {
    filterText = (msg.body || "").trim();
    if (!filterText) return;
  }

  const chat = await msg.getChat();
  const groupId = chat.id._serialized;

  try {
    const result = await callScanner("/api/whatsapp-filter", {
      group_id:       groupId,
      quoted_message: quoted.body,
      filter_text:    filterText,
    });

    if (result.error) {
      console.warn("[WA] Filter webhook error:", result.error);
      await msg.reply(`Couldn't update filter: ${result.error}`);
      return;
    }

    const lines = result.acknowledgements.map(
      (a) => `*${a.customer_name}*:\n_${a.new_filter}_`
    );
    await msg.reply(
      `✅ Filter updated for:\n\n${lines.join("\n\n")}\n\n` +
      `This will apply to all future listing notifications.`
    );
  } catch (err) {
    console.error("[WA] Reply handler call error:", err.message);
    await msg.reply("Error updating filter — is the scanner app running?");
  }
});

// ── Helpers ───────────────────────────────────────────────────────────────────

/** POST JSON to the scanner app and return the parsed response body. */
function callScanner(path, body) {
  return new Promise((resolve, reject) => {
    const payload = JSON.stringify(body);
    const url = new URL(SCANNER_URL + path);
    const lib = url.protocol === "https:" ? https : http;
    const req = lib.request(
      {
        hostname: url.hostname,
        port: url.port || (url.protocol === "https:" ? 443 : 80),
        path: url.pathname,
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(payload),
        },
      },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          try { resolve(JSON.parse(data)); }
          catch { resolve({ error: "Invalid response from scanner" }); }
        });
      }
    );
    req.on("error", reject);
    req.write(payload);
    req.end();
  });
}

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
