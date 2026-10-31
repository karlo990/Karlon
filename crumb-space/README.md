---
title: Sunday Crumb
emoji: 🥐
colorFrom: red
colorTo: yellow
sdk: docker
app_port: 7860
pinned: false
---

# Sunday Crumb: Crumb the assistant

The backend for the Sunday Crumb site's "Ask Crumb" chat, plus the site itself,
Crumb on WhatsApp (with the PC bridge, `crumb-pc-bridge`) and Crumb's kitchen
(the dashboard).

| Path | What |
|---|---|
| `/` | the Sunday Crumb site, with the chat widget calling this API (same origin) |
| `/admin` | Crumb's kitchen: chats, WhatsApp, live feed, settings, try-Crumb, endpoints, setup |
| `/docs` | interactive API docs |
| `/api/endpoints` | every endpoint, live and planned, as JSON |

## Secrets and variables (Settings → Variables and secrets)

| Name | Kind | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | secret | Claude writes Crumb's replies. Without it Crumb answers from the bakery facts. |
| `TURNSTILE_SECRET_KEY` | secret | Cloudflare Turnstile secret for the person check. |
| `TURNSTILE_SITE_KEY` | variable | the matching public site key. The check is on only when both are set. |
| `CRUMB_ADMIN_PASSWORD` | secret | password for `/admin`. Without it the dashboard stays locked. |
| `CRUMB_SECRET` | secret | signs the session cookie, so chats survive a restart (any long random string). |
| `CRUMB_BRIDGE_TOKEN` | secret | shared with the WhatsApp PC bridge (any long random string). Without it WhatsApp is off. |
| `CRUMB_WA_REPLY_WINDOW_MIN` | variable | only answer WhatsApp messages newer than this (default 30). |
| `CRUMB_WA_HUMAN_PAUSE_MIN` | variable | someone answered from the phone or dashboard: Crumb stays quiet this long (default 120). |
| `CRUMB_ALLOWED_ORIGINS` | variable | sites allowed to call the API from a browser (comma-separated). |
| `CRUMB_MODEL` | variable | default `claude-opus-5-5`. |
| `CRUMB_EFFORT` | variable | `low` (default), `medium` or `high`. |
| `CRUMB_DAILY_LIMIT` | variable | model replies per UTC day for everyone (default 2000), then offline answers. |
| `CRUMB_RATE_PER_SESSION` / `CRUMB_RATE_PER_IP` | variable | messages per 10 minutes (default 20 / 40). |

In the Cloudflare Turnstile widget settings, add this Space's hostname
(`quasar-intel-twykers-space.hf.space`) next to the site's own hostname, or the person check fails.

## Endpoints

Live: `GET /api/assistant`, `POST /api/session`, `POST /api/chat`, `GET /api/menu`,
`GET /api/story`, `POST /api/box/quote`, `POST /api/feedback`, `GET /api/health`,
`GET /api/endpoints`.

Planned: these already answer `501` with their contract, so a front end can be built against
them now: `GET /api/specials`, `GET /api/hours`, `POST /api/orders`, `GET /api/orders/{order_id}`,
`POST /api/payments/checkout`, `POST /api/payments/webhook`, `POST /api/newsletter`,
`GET /api/loyalty/{member_id}`, `POST /api/reservations`, `GET /api/chat/stream`, `POST /api/handoff`.

To ship one, implement it in `crumb/api.py` and set its status to `live` in `crumb/endpoints.py`.

## Crumb on WhatsApp

The PC bridge (`crumb-pc-bridge`, on a Windows PC with Crumb's WhatsApp number) reads the recent
chats from WhatsApp Web and sends each one to `POST /api/chats/{id}/sync`. A new message from a
person gets an answer from Crumb, queued in `GET /api/outbox/urgent`; the bridge types it into
WhatsApp within a few seconds and confirms it with `PATCH .../wa-ack`. Every bridge call needs
`Authorization: Bearer <CRUMB_BRIDGE_TOKEN>`.

Crumb stays quiet when the message is older than 30 minutes (so the first sync of old chats sends
nothing), when someone answered from the phone or the dashboard in the last 2 hours, when auto-reply
is off (globally or for that chat), or when a reply is already waiting to go out. The dashboard's
WhatsApp tab shows each chat, why Crumb is quiet, and lets you write to the person yourself.

## How a chat works

1. The widget loads `GET /api/assistant`. If it gets a `turnstileSiteKey`, it runs the person check.
2. `POST /api/session` sends the Turnstile token. The server verifies it with Cloudflare and
   sets a signed 30-minute `crumb_session` cookie.
3. `POST /api/chat` sends the last messages. The server checks the cookie, the rate limits and
   the daily cap, asks Claude, logs the turn for the dashboard and returns `{"text": ...}`.
   If the model is unavailable, Crumb answers from the bakery facts instead.

The free Space disk is temporary, so chats and dashboard settings reset on restart. Permanent
facts live in `crumb/knowledge.py`.

## Local run

```bash
pip install -r requirements.txt
uvicorn crumb.main:app --port 7860
python -m pytest -q tests
```
