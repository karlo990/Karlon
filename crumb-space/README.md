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

The backend for the Sunday Crumb site's "Ask Crumb" chat, plus the site itself and
Crumb's kitchen (the dashboard).

| Path | What |
|---|---|
| `/` | the Sunday Crumb site, with the chat widget calling this API (same origin) |
| `/admin` | Crumb's kitchen: chats, live feed, settings, try-Crumb, endpoints, setup |
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
