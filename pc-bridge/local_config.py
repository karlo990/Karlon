"""local_config.py — machine-specific settings for the PC-side Karlon scripts
(wa_bridge.py, invoice_worker.py, airbnb_parallel_system.py, karlon_supervisor.py).

This file is intentionally the ONE place the URL and local paths live, so all
four scripts agree on which server they talk to. Every value can be overridden
with an environment variable of the same name, which is how karlon_supervisor.py
passes KARLON_URL down to the processes it spawns.

Copy this to the machine that runs the bridge and edit the defaults if needed;
nothing here is secret.
"""

import os

# ── The Karlon server every script talks to ────────────────────────────────
# Default is the HuggingFace Space. Point at http://127.0.0.1:8000 (or
# http://localhost:8000) when running the FastAPI app locally.
KARLON_URL = os.environ.get("KARLON_URL", "https://davincii-code-karlcon.hf.space").rstrip("/")

# ── Polling cadences (seconds) ─────────────────────────────────────────────
WA_POLL_INTERVAL      = int(os.environ.get("WA_POLL_INTERVAL", "5"))       # WA→Karlon import cycle
WA_OUTBOX_POLL_SEC    = int(os.environ.get("WA_OUTBOX_POLL_SEC", "5"))     # outbox REST poll
INVOICE_POLL_SEC      = int(os.environ.get("INVOICE_POLL_SEC", "5"))       # invoice_worker poll

# ── Scraper ────────────────────────────────────────────────────────────────
AIRBNB_LOGIN_TIMEOUT_SECONDS = int(os.environ.get("AIRBNB_LOGIN_TIMEOUT_SECONDS", "360"))

# ── Paths ──────────────────────────────────────────────────────────────────
LOG_DIR = os.environ.get("LOG_DIR", "./logs")
# LibreOffice's soffice executable (invoice_worker.py converts docx→pdf with it).
# Only used as a fallback if `soffice` isn't already on PATH.
LIBREOFFICE_PATH = os.environ.get(
    "LIBREOFFICE_PATH",
    r"C:\Program Files\LibreOffice\program\soffice.exe",
)


def print_active_config() -> None:
    print(
        "[local_config] KARLON_URL=%s  WA_POLL=%ss  OUTBOX_POLL=%ss  INVOICE_POLL=%ss  LOG_DIR=%s"
        % (KARLON_URL, WA_POLL_INTERVAL, WA_OUTBOX_POLL_SEC, INVOICE_POLL_SEC, LOG_DIR)
    )
