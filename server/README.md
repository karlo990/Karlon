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
