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

# How many chats each pass reads: the N most recent in WhatsApp's own order.
# 0 = the old behaviour (every chat in the sidebar, filtered by should_sync).
WA_SYNC_LAST_N_CHATS  = _int_env("WA_SYNC_LAST_N_CHATS", 15, minimum=0)

# Each synced chat's JSON snapshot (what was read, what was rejected) is
# written here as <chat id>.json — the per-chat audit copy.
CHAT_SNAPSHOT_DIR = os.environ.get("CHAT_SNAPSHOT_DIR", "./chat_snapshots")

# WhatsApp shows local times; they're converted to UTC with this PC's time
# zone. Set to force an offset instead, in minutes (Harare = 120).
WA_UTC_OFFSET_MINUTES = (int(os.environ["WA_UTC_OFFSET_MINUTES"])
                         if os.environ.get("WA_UTC_OFFSET_MINUTES", "").lstrip("-").isdigit() else None)

# Reload WhatsApp Web every N hours (0 = never). A WhatsApp Web tab left
# open for a day grows in memory and slows every scroll/read on the PC.
WA_RELOAD_HOURS = _int_env("WA_RELOAD_HOURS", 6, minimum=0)

# Optional shared secret sent as `Authorization: Bearer <token>` on every request
# (see karlon_client.py). Unset = no header, exactly as before. The server must
# enforce it for it to protect anything.
KARLON_API_TOKEN = os.environ.get("KARLON_API_TOKEN", "")

# ── Scraper ────────────────────────────────────────────────────────────────
AIRBNB_LOGIN_TIMEOUT_SECONDS = _int_env("AIRBNB_LOGIN_TIMEOUT_SECONDS", 360)

# Which cities the scraper sweeps (comma-separated). Empty = the full
# ZIMBABWE_CITIES list in airbnb_parallel_system.py.
AIRBNB_CITIES = [c.strip() for c in os.environ.get("AIRBNB_CITIES", "").split(",") if c.strip()]
# Listings scraped per city per cycle: new ones first, then ones not refreshed
# for AIRBNB_RESCRAPE_HOURS. Each city is searched, scraped and pushed on its
# own, so a city's houses show in the app as soon as that city is done.
AIRBNB_LISTINGS_PER_CITY = _int_env("AIRBNB_LISTINGS_PER_CITY", 10)
AIRBNB_RESCRAPE_HOURS = _int_env("AIRBNB_RESCRAPE_HOURS", 12)
AIRBNB_CITY_WORKERS = _int_env("AIRBNB_CITY_WORKERS", 3)          # cities searched at the same time
AIRBNB_CYCLE_REST_SECONDS = _int_env("AIRBNB_CYCLE_REST_SECONDS", 300, minimum=0)

# ── Reservations (Reserve button in the app → airbnb_reserve.py) ──────────
# TEST MODE unless AIRBNB_RESERVE_LIVE=1: every step runs (listing, Reserve,
# card/total checks, message to host) except the final "Request to book"
# click, and the app shows "Test run OK". Set to 1 only once a test run on a
# real listing looks right.
AIRBNB_RESERVE_LIVE = os.environ.get("AIRBNB_RESERVE_LIVE", "0").strip().lower() in ("1", "true", "yes", "on")
# Never pay more than this per reservation (USD). 0 = no fixed cap; the 10%
# price-change guard against the quoted total still applies.
AIRBNB_RESERVE_MAX_TOTAL_USD = _int_env("AIRBNB_RESERVE_MAX_TOTAL_USD", 0, minimum=0)
# How often the scraper asks the server for a queued reservation.
AIRBNB_RESERVE_POLL_SEC = _int_env("AIRBNB_RESERVE_POLL_SEC", 15, minimum=5)

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
        "[local_config] KARLON_URL=%s  WA_POLL=%ss  OUTBOX_POLL=%ss  INVOICE_POLL=%ss  "
        "LAST_N_CHATS=%s  LOG_DIR=%s"
        % (KARLON_URL, WA_POLL_INTERVAL, WA_OUTBOX_POLL_SEC, INVOICE_POLL_SEC,
           WA_SYNC_LAST_N_CHATS, LOG_DIR)
    )
