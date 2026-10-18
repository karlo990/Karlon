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
from pathlib import Path


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

# ── Always-on running (karlon_supervisor.py, install_karlon.ps1) ──────────
# Restart a process when it and its browsers together use more than this many
# MB (0 = no cap). A WhatsApp Web tab grows for days; a restart is the only
# real way to give that memory back.
KARLON_WA_MAX_MB      = _int_env("KARLON_WA_MAX_MB", 2500, minimum=0)
KARLON_SCRAPER_MAX_MB = _int_env("KARLON_SCRAPER_MAX_MB", 3000, minimum=0)
KARLON_INVOICE_MAX_MB = _int_env("KARLON_INVOICE_MAX_MB", 1200, minimum=0)
# Fresh start for each process after this many hours up (0 = never), one at a time.
KARLON_RECYCLE_HOURS  = _int_env("KARLON_RECYCLE_HOURS", 24, minimum=0)
# The network was down at least this long → restart the browser processes
# once it's back, instead of trusting pages that sat through the outage.
KARLON_OUTAGE_RESTART_SEC = _int_env("KARLON_OUTAGE_RESTART_SEC", 180, minimum=0)
# The whole PC is using this % of its memory → restart the biggest process.
KARLON_SYS_MEM_PCT    = _int_env("KARLON_SYS_MEM_PCT", 92)
# Stop Windows idle-sleeping while the supervisor runs (1 = on).
KARLON_KEEP_AWAKE     = os.environ.get("KARLON_KEEP_AWAKE", "1").strip() not in ("0", "false", "no", "off")

# ── Headless after a one-time visible login (python karlon_login.py) ──────
# WhatsApp and Airbnb need a person once (QR scan, Airbnb login); after that
# the saved login lets them run headless, in the background, with no window.
#   auto (default) = headless once that app's login is saved, a visible window
#                    while it isn't (or after it expires);
#   1 = always headless;  0 = always a visible window.
KARLON_WA_HEADLESS     = os.environ.get("KARLON_WA_HEADLESS", "auto").strip().lower()
KARLON_AIRBNB_HEADLESS = os.environ.get("KARLON_AIRBNB_HEADLESS", "auto").strip().lower()


def headless_choice(setting: str, login_saved: bool) -> bool:
    if setting in ("1", "true", "yes", "on"):
        return True
    if setting in ("0", "false", "no", "off"):
        return False
    return bool(login_saved)


def wa_session_dir() -> str:
    """Where the WhatsApp login (the Chromium profile) is kept. It used to be
    ./wa_session, relative to wherever the script was started, so every
    unzip of a new version into a new folder started logged out. Now:
    WA_SESSION_DIR if set, else ~/.karlon/wa_session (the same place whatever
    folder the scripts run from). An existing ./wa_session is still used when
    there is no home-folder session yet, so old setups keep their login."""
    env = os.environ.get("WA_SESSION_DIR", "").strip()
    if env:
        return str(Path(env).expanduser().resolve())
    home = Path.home() / ".karlon" / "wa_session"
    legacy = Path("./wa_session")
    if not home.exists() and legacy.exists():
        return str(legacy.resolve())
    return str(home)


def wa_login_marker(session_dir: str | None = None) -> Path:
    """Written by wa_bridge.py when the chat list shows (logged in), removed
    when WhatsApp asks for the QR code again (logged out)."""
    return Path(session_dir or wa_session_dir()) / "karlon_logged_in"


# The scraper's saved Airbnb login (cookies + localStorage), relative to the
# pc-bridge folder like the rest of airbnb_data/.
AIRBNB_AUTH_STATE_FILE = os.path.join("./airbnb_data", "auth_state.json")

# Flags for every Chromium the scripts start. The first three stop Chromium
# slowing a tab down when its window is minimised, covered or the lid is
# closed (WhatsApp Web would otherwise only check for messages about once a
# minute); the cache cap keeps the profile from growing without limit.
CHROMIUM_ARGS = [
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion",
    "--disk-cache-size=104857600",
    "--no-first-run",
    "--no-default-browser-check",
] + os.environ.get("KARLON_CHROMIUM_ARGS", "").split()

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
