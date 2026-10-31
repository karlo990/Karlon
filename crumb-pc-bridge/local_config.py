"""local_config.py — machine-specific settings for the Crumb PC bridge
(crumb_whatsapp.py, crumb_supervisor.py, crumb_login.py).

This file is intentionally the ONE place the URL and local paths live, so the
scripts agree on which server they talk to. Every value can be overridden
with an environment variable of the same name, which is how crumb_supervisor.py
passes CRUMB_URL down to the processes it spawns.

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


# ── The Crumb server every script talks to ────────────────────────────────
# Default is the Sunday Crumb Space on Hugging Face. Point at
# http://127.0.0.1:7860 when running crumb-space locally.
CRUMB_URL = os.environ.get("CRUMB_URL", "https://quasar-intel-twykers-space.hf.space").rstrip("/")

# ── Polling cadences (seconds) ─────────────────────────────────────────────
WA_POLL_INTERVAL      = _int_env("CRUMB_WA_POLL_INTERVAL", 5)          # WA→Crumb import cycle
WA_OUTBOX_POLL_SEC    = _int_env("CRUMB_WA_OUTBOX_POLL_SEC", 5)        # outbox REST poll

# Every Nth WA→Crumb pass re-reads ALL chats even if their sidebar row looks
# unchanged (bounded staleness for the incremental sync — see crumb_whatsapp.sync_once).
# 1 = always full sweep (the old behaviour).
WA_FULL_SWEEP_EVERY   = _int_env("CRUMB_WA_FULL_SWEEP_EVERY", 10)

# How many chats each pass reads: the N most recent in WhatsApp's own order.
# 0 = the old behaviour (every chat in the sidebar, filtered by should_sync).
WA_SYNC_LAST_N_CHATS  = _int_env("CRUMB_WA_SYNC_LAST_N_CHATS", 15, minimum=0)

# Each synced chat's JSON snapshot (what was read, what was rejected) is
# written here as <chat id>.json — the per-chat audit copy.
CHAT_SNAPSHOT_DIR = os.environ.get("CRUMB_CHAT_SNAPSHOT_DIR", "./chat_snapshots")

# WhatsApp shows local times; they're converted to UTC with this PC's time
# zone. Set to force an offset instead, in minutes (Harare = 120).
WA_UTC_OFFSET_MINUTES = (int(os.environ["CRUMB_WA_UTC_OFFSET_MINUTES"])
                         if os.environ.get("CRUMB_WA_UTC_OFFSET_MINUTES", "").lstrip("-").isdigit() else None)

# Reload WhatsApp Web every N hours (0 = never). A WhatsApp Web tab left
# open for a day grows in memory and slows every scroll/read on the PC.
WA_RELOAD_HOURS = _int_env("CRUMB_WA_RELOAD_HOURS", 6, minimum=0)

# The shared secret sent as `Authorization: Bearer <token>` on every request
# (see crumb_client.py). The Crumb server refuses the bridge without it: set
# the same value as the Space's CRUMB_BRIDGE_TOKEN secret (install_crumb.ps1
# -BridgeToken saves it for your user).
CRUMB_BRIDGE_TOKEN = os.environ.get("CRUMB_BRIDGE_TOKEN", "")

# ── Always-on running (crumb_supervisor.py, install_crumb.ps1) ──────────
# Restart a process when it and its browsers together use more than this many
# MB (0 = no cap). A WhatsApp Web tab grows for days; a restart is the only
# real way to give that memory back.
CRUMB_WA_MAX_MB      = _int_env("CRUMB_WA_MAX_MB", 2500, minimum=0)
# Fresh start for each process after this many hours up (0 = never), one at a time.
CRUMB_RECYCLE_HOURS  = _int_env("CRUMB_RECYCLE_HOURS", 24, minimum=0)
# The network was down at least this long → restart the browser processes
# once it's back, instead of trusting pages that sat through the outage.
CRUMB_OUTAGE_RESTART_SEC = _int_env("CRUMB_OUTAGE_RESTART_SEC", 180, minimum=0)
# The whole PC is using this % of its memory → restart the biggest process.
CRUMB_SYS_MEM_PCT    = _int_env("CRUMB_SYS_MEM_PCT", 92)
# Stop Windows idle-sleeping while the supervisor runs (1 = on).
CRUMB_KEEP_AWAKE     = os.environ.get("CRUMB_KEEP_AWAKE", "1").strip() not in ("0", "false", "no", "off")

# ── Headless after a one-time visible login (python crumb_login.py) ──────
# WhatsApp needs a person once (the QR scan); after that the saved login
# lets it run headless, in the background, with no window.
#   auto (default) = headless once the login is saved, a visible window
#                    while it isn't (or after it expires);
#   1 = always headless;  0 = always a visible window (use this if WhatsApp
#   reads chats in a window but not headless).
CRUMB_WA_HEADLESS     = os.environ.get("CRUMB_WA_HEADLESS", "auto").strip().lower()


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
    CRUMB_WA_SESSION_DIR if set, else ~/.crumb/wa_session (the same place whatever
    folder the scripts run from). An existing ./wa_session is still used when
    there is no home-folder session yet, so old setups keep their login."""
    env = os.environ.get("CRUMB_WA_SESSION_DIR", "").strip()
    if env:
        return str(Path(env).expanduser().resolve())
    home = Path.home() / ".crumb" / "wa_session"
    legacy = Path("./wa_session")
    if not home.exists() and legacy.exists():
        return str(legacy.resolve())
    return str(home)


def wa_login_marker(session_dir: str | None = None) -> Path:
    """Written by crumb_whatsapp.py when the chat list shows (logged in), removed
    when WhatsApp asks for the QR code again (logged out)."""
    return Path(session_dir or wa_session_dir()) / "crumb_logged_in"


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
] + os.environ.get("CRUMB_CHROMIUM_ARGS", "").split()

# ── Paths ──────────────────────────────────────────────────────────────────
LOG_DIR = os.environ.get("CRUMB_LOG_DIR", "./logs")


def print_active_config() -> None:
    print(
        "[local_config] CRUMB_URL=%s  WA_POLL=%ss  OUTBOX_POLL=%ss  LAST_N_CHATS=%s  LOG_DIR=%s  token=%s"
        % (CRUMB_URL, WA_POLL_INTERVAL, WA_OUTBOX_POLL_SEC, WA_SYNC_LAST_N_CHATS, LOG_DIR,
           "set" if CRUMB_BRIDGE_TOKEN else "MISSING (set CRUMB_BRIDGE_TOKEN)")
    )
