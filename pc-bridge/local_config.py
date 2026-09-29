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


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    """Env-var int with a floor. A typo'd or zero value used to either crash the
    script at import (ValueError) or turn a poll loop into a busy-spin."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        print(f"[local_config] ignoring non-integer {name}={raw!r}, using {default}")
        return default


# ── The Karlon server every script talks to ────────────────────────────────
# Default is the HuggingFace Space. Point at http://127.0.0.1:8000 (or
# http://localhost:8000) when running the FastAPI app locally.
KARLON_URL = os.environ.get("KARLON_URL", "https://davincii-code-karlcon.hf.space").rstrip("/")

# ── Polling cadences (seconds) ─────────────────────────────────────────────
WA_POLL_INTERVAL      = _int_env("WA_POLL_INTERVAL", 5)          # WA→Karlon import cycle
WA_OUTBOX_POLL_SEC    = _int_env("WA_OUTBOX_POLL_SEC", 5)        # outbox REST poll
INVOICE_POLL_SEC      = _int_env("INVOICE_POLL_SEC", 5)          # invoice_worker poll

# Every Nth WA→Karlon pass re-reads ALL chats even if their sidebar row looks
# unchanged (bounded staleness for the incremental sync — see wa_bridge.sync_once).
# 1 = always full sweep (the old behaviour).
WA_FULL_SWEEP_EVERY   = _int_env("WA_FULL_SWEEP_EVERY", 10)

# Optional shared secret sent as `Authorization: Bearer <token>` on every request
# (see karlon_client.py). Unset = no header, exactly as before. The server must
# enforce it for it to protect anything.
KARLON_API_TOKEN = os.environ.get("KARLON_API_TOKEN", "")

# ── Scraper ────────────────────────────────────────────────────────────────
AIRBNB_LOGIN_TIMEOUT_SECONDS = _int_env("AIRBNB_LOGIN_TIMEOUT_SECONDS", 360)

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
