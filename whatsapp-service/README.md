# whatsapp-service

A self-contained Node.js HTTP microservice that sends WhatsApp messages and
handles incoming WhatsApp commands.

Uses **[whatsapp-web.js](https://github.com/pedroslopez/whatsapp-web.js)**,
which drives a real Chrome browser running WhatsApp Web. Messages travel
**directly from this machine to WhatsApp's servers** — no third-party service
involved.

---

## Requirements

- Node.js 18+
- A personal WhatsApp account (linked via QR on first run)
- Chrome/Chromium (puppeteer downloads one automatically; or point to your own via `CHROME_PATH`)

---

## Setup (first run)

```bash
cd whatsapp-service
npm install
npm start
```

On first start the service prints a QR code in the terminal **and** serves one
at `http://localhost:3001/qr`.

1. Open WhatsApp on your phone → **Linked Devices** → **Link a Device**
2. Scan the QR code
3. The service logs `[WA] Connected and ready.` — done

The session is saved in `auth_data/` and reused on every subsequent start.
**Do not delete `auth_data/`** unless you want to re-pair.

---

## Environment variables

| Variable      | Default          | Description |
|---------------|------------------|-------------|
| `PORT`        | `3001`           | HTTP port |
| `API_TOKEN`   | _(none)_         | If set, all requests must include `Authorization: Bearer <token>` |
| `CHROME_PATH` | _(auto)_         | Path to Chrome/Chromium binary; puppeteer's bundled binary is used if unset |

Example:

```bash
API_TOKEN=mysecrettoken CHROME_PATH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" npm start
```

---

## HTTP API

### `GET /health`

```json
{ "connected": true, "waiting_for_qr": false }
```

---

### `GET /qr`

HTML page with QR code image (auto-refreshes every 20 s). Only needed before
the first pairing or after a logout.

---

### `POST /send`

Send a text message to a group or individual.

**Body**

```json
{
  "chatId": "120363407400776027@g.us",
  "message": "*New listing!*\nKeizersgracht 123 — €2.000/mo"
}
```

| Field     | Format | Description |
|-----------|--------|-------------|
| `chatId`  | `…@g.us` for groups, `…@c.us` for individuals | Target chat |
| `message` | plain text; `*bold*` and `_italic_` render in WhatsApp | Message body |

**Response:** `{ "ok": true }`

---

### `GET /groups`

List all groups the linked account belongs to (use this to find `chatId`
values to configure on subscribers in the scanner UI).

```json
{
  "groups": [
    { "id": "120363407400776027@g.us", "name": "Apartment Hunt", "participants": 3 },
    { "id": "120363043051405349@g.us", "name": "Family Group",   "participants": 7 }
  ]
}
```

---

## Adding WhatsApp commands

The service has a built-in command system. Incoming WhatsApp messages are
matched against registered commands and dispatched automatically.

### Built-in commands

| Command | Description |
|---------|-------------|
| `!ping` | Replies with `pong` — smoke-test that the bot is alive |
| `!help` | Lists all registered commands |

### Registering a new command

Open `index.js` and add before `wa.init()`:

```javascript
const commands = require('./commands')

commands.register(
  '!listings',                          // trigger (case-insensitive prefix)
  async (msg, client, args) => {        // handler
    // args = everything after '!listings ', trimmed
    await msg.reply('Fetching latest listings…')
    // call your scanner API, query the DB, etc.
  },
  'Show recent listings'                // description shown in !help
)
```

Or put each command in its own file and `require()` it in `index.js`:

```javascript
// commands/listings.js
const commands = require('../commands')
commands.register('!listings', async (msg, client, args) => { … }, 'Show recent listings')

// index.js
require('./commands/listings')
```

### Handler API

```javascript
async (msg, client, args) => {
  // msg    — whatsapp-web.js Message object
  msg.reply('text')           // reply in same chat
  msg.react('👍')             // react with emoji

  // client — the WhatsApp Client instance
  client.sendMessage(chatId, 'text')  // send to any chat

  // args   — string after the trigger, trimmed (may be empty)
}
```

Full Message and Client docs: https://docs.wwebjs.dev

### Fallback handler

To respond to all messages that don't match any command:

```javascript
commands.setDefault(async (msg, client) => {
  await msg.reply("I don't understand that. Type *!help* for available commands.")
})
```

---

## Process management

**pm2** (recommended)

```bash
npm install -g pm2
API_TOKEN=mysecrettoken pm2 start index.js --name whatsapp-service
pm2 save
pm2 startup   # follow the printed command to auto-start on reboot
```

---

## Reconnection behaviour

| Event | Behaviour |
|-------|-----------|
| Temporary network drop / browser crash | Automatically reinitialises after 5 s |
| WhatsApp logout (phone unlinks device) | Logs a warning, does **not** reconnect — delete `auth_data/` and restart to re-pair |

---

## Dependency on the Python scanner

The scanner calls this service via:

```
POST http://localhost:3001/send
Authorization: Bearer <whatsapp_service_token>
```

`config.json` (scanner side):

```json
{
  "notifications": {
    "whatsapp_service_url": "http://localhost:3001",
    "whatsapp_service_token": ""
  }
}
```

Leave `whatsapp_service_token` empty when `API_TOKEN` is not set.
