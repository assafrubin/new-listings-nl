# whatsapp-service

A self-contained Node.js HTTP microservice that sends WhatsApp messages using
[Baileys](https://github.com/WhiskeySockets/Baileys) — the open-source
WhatsApp Web protocol implementation.

Messages travel **directly from this machine to WhatsApp's servers**. No
third-party service (e.g. Green-API, Twilio) is involved, so no message
content is shared with anyone except WhatsApp itself.

---

## Requirements

- Node.js 18+
- A personal WhatsApp account (you'll link it via QR code on first run)

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
3. The service logs `[WA] Connected.` — you're done

Credentials are saved in `auth_info_baileys/` and reused on every subsequent
start. **Do not delete that directory** unless you want to re-pair.

---

## Environment variables

| Variable    | Default | Description |
|-------------|---------|-------------|
| `PORT`      | `3001`  | HTTP port the service listens on |
| `API_TOKEN` | _(none)_ | If set, all requests must include `Authorization: Bearer <token>` |

Example:

```bash
API_TOKEN=mysecrettoken PORT=3001 npm start
```

---

## HTTP API

### `GET /health`

Returns connection status.

```json
{ "connected": true, "waiting_for_qr": false }
```

---

### `GET /qr`

Returns an HTML page with the QR code (auto-refreshes every 20 s). Only
relevant before the first pairing or after a logout.

---

### `POST /send`

Send a message to a WhatsApp group or individual chat.

**Request body**

```json
{
  "chatId": "120363043051405349@g.us",
  "message": "Hello from the scanner!"
}
```

| Field     | Format | Description |
|-----------|--------|-------------|
| `chatId`  | `...@g.us` for groups, `...@s.whatsapp.net` for individuals | Target chat |
| `message` | plain text; `*bold*` and `_italic_` work in WhatsApp | Message body |

**Response**

```json
{ "ok": true }
```

**Error responses**

| Status | Meaning |
|--------|---------|
| `400`  | Missing / invalid body fields |
| `401`  | Bad or missing `Authorization` header (only when `API_TOKEN` is set) |
| `503`  | Not yet connected to WhatsApp (scan QR first) |
| `500`  | Baileys threw an error sending the message |

---

### `GET /groups`

List all WhatsApp groups the linked account is a member of. Use this to find
the `chatId` values to set on subscribers in the scanner UI.

**Response**

```json
{
  "groups": [
    { "id": "120363043051405349@g.us", "name": "Apartment Hunt", "participants": 3 },
    { "id": "120363407400776027@g.us", "name": "Family Group",   "participants": 7 }
  ]
}
```

---

## How to find a group's chatId

1. Make sure the service is running and connected
2. Call `GET /groups` (or open `/groups` in a browser)
3. Copy the `id` of the group you want
4. Paste it into the **WhatsApp group** field on the subscriber in the scanner UI

---

## Keeping the service running

Use any process manager. Simple examples:

**pm2** (recommended)

```bash
npm install -g pm2
API_TOKEN=mysecrettoken pm2 start index.js --name whatsapp-service
pm2 save
pm2 startup   # follow the printed command to auto-start on reboot
```

**systemd** (Linux)

```ini
[Unit]
Description=WhatsApp microservice
After=network.target

[Service]
WorkingDirectory=/path/to/whatsapp-service
ExecStart=/usr/bin/node index.js
Restart=always
Environment=PORT=3001
Environment=API_TOKEN=mysecrettoken

[Install]
WantedBy=multi-user.target
```

---

## Reconnection behaviour

| Event | Behaviour |
|-------|-----------|
| Temporary network drop | Automatically reconnects after 3 s |
| WhatsApp logout (phone unlinks device) | Logs a message, does **not** reconnect — delete `auth_info_baileys/` and restart to re-pair |

---

## Dependency on the scanner

The Python scanner (`scanner.py`) calls this service via:

```
POST http://localhost:3001/send
Authorization: Bearer <whatsapp_service_token>
```

Config in `config.json` (scanner side):

```json
{
  "notifications": {
    "whatsapp_service_url": "http://localhost:3001",
    "whatsapp_service_token": ""
  }
}
```

Leave `whatsapp_service_token` empty if `API_TOKEN` is not set on the service.
