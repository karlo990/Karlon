"""crumb/config.py — every setting, from the Space's variables and secrets.

Secrets (Settings → Variables and secrets → New secret):
    ANTHROPIC_API_KEY      Claude writes Crumb's replies. Without it Crumb answers
                           from the bakery facts alone (offline mode).
    TURNSTILE_SECRET_KEY   Cloudflare Turnstile secret for the "are you a person"
                           check. Without it the check is off (rate limits still apply).
    CRUMB_ADMIN_PASSWORD   password for the /admin dashboard. Without it the
                           dashboard stays locked.
    CRUMB_SECRET           signs the chat-session cookie (random per restart if unset).

Variables:
    TURNSTILE_SITE_KEY     the public site key paired with the secret.
    CRUMB_MODEL            default claude-opus-5-5
    CRUMB_EFFORT           low | medium | high (default low: short chat replies)
    CRUMB_ALLOWED_ORIGINS  comma-separated sites allowed to call the API from a browser
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path


def _int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except ValueError:
        return default


VERSION = "1.0.0"
ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
DB_PATH = Path(os.environ.get("CRUMB_DB_PATH", "/tmp/crumb.db"))

# ── the model ───────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("CRUMB_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("CRUMB_EFFORT", "low") if os.environ.get("CRUMB_EFFORT", "low") in (
    "low", "medium", "high", "xhigh", "max") else "low"
MAX_TOKENS = _int("CRUMB_MAX_TOKENS", 16000, 512)
LLM_TIMEOUT_SEC = _int("CRUMB_LLM_TIMEOUT_SEC", 60, 5)
# Server-side fallback when the model declines (refusal): Anthropic picks the
# right substitute per refusal category.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# ── the "are you a person" check ────────────────────────────────────────────
TURNSTILE_SITE_KEY = os.environ.get("TURNSTILE_SITE_KEY", "")
TURNSTILE_SECRET_KEY = os.environ.get("TURNSTILE_SECRET_KEY", "")
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TURNSTILE_ON = bool(TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY)
SESSION_COOKIE = "crumb_session"
SESSION_MINUTES = _int("CRUMB_SESSION_MINUTES", 30, 1)
SECRET = os.environ.get("CRUMB_SECRET") or secrets.token_hex(32)

# ── limits (a public widget: keep it cheap and abuse-proof) ─────────────────
MAX_SENT = 12                    # messages per request (the widget sends at most 12)
MAX_CHARS = 1000                 # per message (the widget caps input at 1000)
RATE_PER_SESSION = _int("CRUMB_RATE_PER_SESSION", 20, 1)   # messages per window
RATE_PER_IP = _int("CRUMB_RATE_PER_IP", 40, 1)
RATE_WINDOW_SEC = _int("CRUMB_RATE_WINDOW_SEC", 600, 10)
DAILY_LIMIT = _int("CRUMB_DAILY_LIMIT", 2000, 1)           # model replies per UTC day, all visitors

# ── the dashboard ───────────────────────────────────────────────────────────
ADMIN_PASSWORD = os.environ.get("CRUMB_ADMIN_PASSWORD", "")
ADMIN_COOKIE = "crumb_admin"

ALLOWED_ORIGINS = [o.strip().rstrip("/") for o in os.environ.get(
    "CRUMB_ALLOWED_ORIGINS", "https://sunday-crumb.mafatakesure.workers.dev").split(",") if o.strip()]
