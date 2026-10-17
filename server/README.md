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
creates the invoice and sends the Terms & Conditions. Its voice and every
line it says are in `app/davis/persona.py`; its model is a small local one
(llama.cpp on the Space's CPU, no external API). Dashboard at `/davis`.
Starts in shadow mode (drafts for staff to approve). Setup, rules and
architecture: [DAVIS.md](DAVIS.md).
