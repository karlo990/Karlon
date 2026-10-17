"""davis/config.py — Davis's knobs. Env vars give the defaults; the dashboard
can change mode / provider / model at runtime (stored in davis_settings, so a
change survives a restart until the Space rebuild wipes the DB)."""

import os


def _int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except ValueError:
        return default


def _float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, default)))
    except ValueError:
        return default


# off    — Davis does nothing.
# shadow — Davis reads every message and writes what it WOULD send as a draft
#          on the dashboard; nothing reaches WhatsApp until staff approve it.
# live   — Davis replies on WhatsApp by itself.
MODES = ("off", "shadow", "live")
MODE = os.environ.get("DAVIS_MODE", "shadow").strip().lower()
if MODE not in MODES:
    MODE = "shadow"

# anthropic — Claude API (needs ANTHROPIC_API_KEY). Fast enough for live chat.
# worker    — the Conscious Worker Space (/api/jarvis/conscious, phi4-mini on
#             CPU). Too slow for live chat (about 1 token/s); kept as an option.
# ollama    — any Ollama server's /api/chat.
# rules     — no model: fixed templates. Always available, also the fallback.
PROVIDERS = ("anthropic", "worker", "ollama", "rules")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
PROVIDER = os.environ.get("DAVIS_PROVIDER", "anthropic" if ANTHROPIC_API_KEY else "rules").strip().lower()
if PROVIDER not in PROVIDERS:
    PROVIDER = "rules"
MODEL = os.environ.get("DAVIS_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("DAVIS_EFFORT", "low")
LLM_TIMEOUT_SEC = _float("DAVIS_LLM_TIMEOUT_SEC", 25.0, 3.0)
LLM_MAX_TOKENS = _int("DAVIS_LLM_MAX_TOKENS", 2048, 256)

WORKER_URL = os.environ.get("DAVIS_WORKER_URL", "https://karl-con-conscious-worker-alpha.hf.space").rstrip("/")
WORKER_KEY = os.environ.get("DAVIS_WORKER_KEY", os.environ.get("INTERNAL_API_KEY", ""))
OLLAMA_URL = os.environ.get("DAVIS_OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("DAVIS_OLLAMA_MODEL", "phi4-mini:3.8b-q4_K_M")

# ── conversation limits (the user's rules) ─────────────────────────────────
MAX_WORDS = _int("DAVIS_MAX_WORDS", 40, 10)                 # per Davis message
MAX_REPLIES_PER_TURN = _int("DAVIS_MAX_REPLIES_PER_TURN", 2, 1)  # then wait for the guest
OPTIONS_PER_SEND = _int("DAVIS_OPTIONS_PER_SEND", 3, 1)
IMAGES_PER_OPTION = _int("DAVIS_IMAGES_PER_OPTION", 3, 1)

# ── when Davis stays quiet ─────────────────────────────────────────────────
DEBOUNCE_SEC = _float("DAVIS_DEBOUNCE_SEC", 8.0)            # let the guest finish typing
FRESH_MINUTES = _int("DAVIS_FRESH_MINUTES", 15, 1)          # never answer older messages
HUMAN_QUIET_MINUTES = _int("DAVIS_HUMAN_QUIET_MINUTES", 30)  # staff replied → step back
HISTORY_MESSAGES = _int("DAVIS_HISTORY_MESSAGES", 24, 4)    # what the model sees

# ── process management ─────────────────────────────────────────────────────
CONCURRENCY = _int("DAVIS_CONCURRENCY", 4, 1)               # conversations at once
SWEEP_SEC = _int("DAVIS_SWEEP_SEC", 30, 5)                  # safety-net scan
BREAKER_FAILURES = _int("DAVIS_BREAKER_FAILURES", 3, 1)
BREAKER_COOLDOWN_SEC = _int("DAVIS_BREAKER_COOLDOWN_SEC", 60, 5)
CACHE_TTL_SEC = _int("DAVIS_CACHE_TTL_SEC", 60, 0)

# Write endpoints (settings, approve, pause…) need this token when it is set.
ADMIN_TOKEN = os.environ.get("DAVIS_ADMIN_TOKEN", "")

# Sender names on what Davis queues. Every one starts with "Davis", which is
# how Davis tells its own messages from staff replies.
SENDER = "Davis"
SENDER_CATALOGUE = "Davis (catalogue)"
SENDER_DOCUMENTS = "Davis (documents)"

BRAND = "KarlCon Elite Retreats"
