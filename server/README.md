---
title: Karlcon
emoji: 💬
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---

# Karlon server

Real-time messaging hub for KARLCON Elite Retreats. FastAPI + SQLite,
serving the Android app and receiving imports from `wa_bridge.py`.

Local run:

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Davis (WhatsApp reception + booking assistant)

Davis answers guests on WhatsApp through the same outbox: qualifies the
stay, sends houses with photos from the live catalogue, collects name and ID,
creates the invoice and sends the Terms & Conditions. Dashboard at `/davis`.
Starts in shadow mode (drafts for staff to approve). Setup, rules and
architecture: [DAVIS.md](DAVIS.md). Secrets: `ANTHROPIC_API_KEY`,
`DAVIS_MODE`, `DAVIS_ADMIN_TOKEN`.
