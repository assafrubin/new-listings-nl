/**
 * commands.js — WhatsApp command registry and dispatcher.
 *
 * Adding a new command
 * --------------------
 * Require this file and call register() anywhere before init() returns:
 *
 *   const commands = require('./commands')
 *   commands.register('!search', async (msg, client, args) => {
 *     // args = everything after '!search '
 *     await msg.reply(`Searching for: ${args}`)
 *   }, 'Search listings by keyword')
 *
 * Trigger matching
 * ----------------
 * - Case-insensitive prefix match: '!ping' matches '!ping', '!PING', '!ping hello'
 * - First registered match wins (register in specificity order, longest first)
 * - A built-in '!help' command lists all registered commands and their descriptions
 *
 * Handler signature
 * -----------------
 *   async (msg, client, args) => void
 *   - msg    : whatsapp-web.js Message object (supports msg.reply(), msg.react(), etc.)
 *   - client : the WhatsApp Client instance (for sending to other chats, etc.)
 *   - args   : string after the trigger, trimmed (may be empty)
 *
 * Default / fallback handler
 * --------------------------
 * Set a handler for messages that match no registered command:
 *   commands.setDefault(async (msg, client) => { ... })
 * If none is set, unrecognised commands are silently ignored.
 */

"use strict";

/** @type {Array<{ trigger: string, handler: Function, description: string }>} */
const registry = [];

/** @type {Function|null} */
let defaultHandler = null;

/**
 * Register a command handler.
 * @param {string}   trigger      Prefix to match (e.g. '!ping')
 * @param {Function} handler      async (msg, client, args) => void
 * @param {string}   [description] Short description shown in !help
 */
function register(trigger, handler, description = "") {
  registry.push({ trigger: trigger.toLowerCase(), handler, description });
}

/**
 * Set a fallback handler called when no registered command matches.
 * @param {Function} handler  async (msg, client) => void
 */
function setDefault(handler) {
  defaultHandler = handler;
}

/**
 * Dispatch an incoming message to the matching command handler.
 * @param {import('whatsapp-web.js').Message} msg
 * @param {import('whatsapp-web.js').Client}  client
 * @returns {Promise<boolean>} true if a handler was invoked
 */
async function dispatch(msg, client) {
  const body = (msg.body || "").trim();
  const lower = body.toLowerCase();

  for (const { trigger, handler, description: _d } of registry) {
    if (lower === trigger || lower.startsWith(trigger + " ")) {
      const args = body.slice(trigger.length).trim();
      try {
        await handler(msg, client, args);
      } catch (err) {
        console.error(`[CMD] Error in handler for "${trigger}":`, err.message);
      }
      return true;
    }
  }

  if (defaultHandler) {
    try {
      await defaultHandler(msg, client);
    } catch (err) {
      console.error("[CMD] Error in default handler:", err.message);
    }
    return true;
  }

  return false;
}

// ── Built-in commands ─────────────────────────────────────────────────────────

register(
  "!ping",
  async (msg) => {
    await msg.reply("pong");
  },
  "Check the bot is alive"
);

register(
  "!help",
  async (msg) => {
    if (registry.length === 0) {
      await msg.reply("No commands registered.");
      return;
    }
    const lines = registry.map(
      ({ trigger, description }) =>
        `*${trigger}*${description ? " — " + description : ""}`
    );
    await msg.reply("*Available commands:*\n" + lines.join("\n"));
  },
  "List available commands"
);

module.exports = { register, setDefault, dispatch };
