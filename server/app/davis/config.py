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

# local  — a small instruct model run in this server process with llama.cpp
#          (GGUF file, CPU). No API, no key, nothing leaves the Space.
# ollama — an Ollama server's /api/chat (e.g. on a PC with a GPU).
# worker — the Conscious Worker Space (/api/jarvis/conscious, phi4-mini).
# rules  — no model: the persona's fixed lines. Always available, and the
#          fallback whenever a model is slow, down or its reply fails the guard.
PROVIDERS = ("local", "ollama", "worker", "rules")
PROVIDER = os.environ.get("DAVIS_PROVIDER", "local").strip().lower()
if PROVIDER not in PROVIDERS:
    PROVIDER = "local"

# The local model. Default: Qwen2.5 1.5B Instruct, 4-bit (about 1.1 GB, ~2 GB
# RAM). On a 2-vCPU Space a reply takes roughly 6-12 s; 3B models write
# better and take about twice as long. The Dockerfile downloads the file at
# build time; if it is missing at start it is downloaded once.
LOCAL_MODEL_REPO = os.environ.get("DAVIS_LOCAL_MODEL_REPO", "Qwen/Qwen2.5-1.5B-Instruct-GGUF")
LOCAL_MODEL_FILE = os.environ.get("DAVIS_LOCAL_MODEL_FILE", "qwen2.5-1.5b-instruct-q4_k_m.gguf")
LOCAL_MODEL_DIR = os.environ.get("DAVIS_LOCAL_MODEL_DIR",
                                 "/models" if os.path.isdir("/models") else
                                 os.path.join(os.path.dirname(__file__), "..", "..", "models"))
LOCAL_THREADS = _int("DAVIS_LOCAL_THREADS", 0)          # 0 = the cores this container really has
LOCAL_CTX = _int("DAVIS_LOCAL_CTX", 4096, 1024)
LOCAL_MAX_TOKENS = _int("DAVIS_LOCAL_MAX_TOKENS", 220, 40)   # 40 words + JSON; digits are a token each
LOCAL_TEMPERATURE = _float("DAVIS_LOCAL_TEMPERATURE", 0.3)
LOCAL_PRELOAD = os.environ.get("DAVIS_LOCAL_PRELOAD", "1") not in ("0", "false", "no")
LLM_TIMEOUT_SEC = _float("DAVIS_LLM_TIMEOUT_SEC", 45.0, 3.0)   # then the persona line goes out

WORKER_URL = os.environ.get("DAVIS_WORKER_URL", "https://karl-con-conscious-worker-alpha.hf.space").rstrip("/")
WORKER_KEY = os.environ.get("DAVIS_WORKER_KEY", os.environ.get("INTERNAL_API_KEY", ""))
OLLAMA_URL = os.environ.get("DAVIS_OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("DAVIS_OLLAMA_MODEL", "qwen2.5:3b-instruct")

# ── conversation limits (the user's rules) ─────────────────────────────────
MAX_WORDS = _int("DAVIS_MAX_WORDS", 40, 10)                 # per Davis message
MAX_REPLIES_PER_TURN = _int("DAVIS_MAX_REPLIES_PER_TURN", 2, 1)  # then wait for the guest
# The invoice adds this service fee (pc-bridge/invoice_worker.py,
# INVOICE_SERVICE_FEE_PCT): Davis quotes the same balance the invoice shows.
SERVICE_FEE_PCT = _float("DAVIS_SERVICE_FEE_PCT", 15.0)
OPTIONS_PER_SEND = _int("DAVIS_OPTIONS_PER_SEND", 3, 1)
IMAGES_PER_OPTION = _int("DAVIS_IMAGES_PER_OPTION", 3, 1)

# ── when Davis stays quiet ─────────────────────────────────────────────────
DEBOUNCE_SEC = _float("DAVIS_DEBOUNCE_SEC", 8.0)            # let the guest finish typing
FRESH_MINUTES = _int("DAVIS_FRESH_MINUTES", 3 * 24 * 60, 1)  # answer guests who wrote in the last 3 days
# 0 = Davis answers every guest message, even right after a staff reply (Davis
# always has the last word; "Take over" on the dashboard mutes a chat).
HUMAN_QUIET_MINUTES = _int("DAVIS_HUMAN_QUIET_MINUTES", 0)
HISTORY_MESSAGES = _int("DAVIS_HISTORY_MESSAGES", 24, 4)    # what Davis reads per turn
# Where the model writes: "open" = only answers to open questions (from the
# team's house notes); every booking step uses the persona's lines (instant,
# exact). "all" = the model rewrites every reply too (slower; each rewrite
# must still pass the guard). Changeable on the dashboard.
#   ai   = the model writes every reply itself (no fixed sentences reach guests;
#          if it can't write a safe one, the reply waits for staff). Default.
LLM_FOR = os.environ.get("DAVIS_LLM_FOR", "ai") if os.environ.get("DAVIS_LLM_FOR", "ai") in ("open", "all", "ai") \
    else "ai"
MODEL_HISTORY = _int("DAVIS_MODEL_HISTORY", 10, 2)          # of which the model sees the last N

# ── process management ─────────────────────────────────────────────────────
CONCURRENCY = _int("DAVIS_CONCURRENCY", 4, 1)               # conversations at once
SWEEP_SEC = _int("DAVIS_SWEEP_SEC", 30, 5)                  # safety-net scan
BREAKER_FAILURES = _int("DAVIS_BREAKER_FAILURES", 3, 1)
BREAKER_COOLDOWN_SEC = _int("DAVIS_BREAKER_COOLDOWN_SEC", 60, 5)
CACHE_TTL_SEC = _int("DAVIS_CACHE_TTL_SEC", 60, 0)

# Chat lists that survive restarts (the dashboard's copies live in the DB,
# which a Space rebuild wipes). Comma-separated chat ids, e.g. wa-b5ead6a2ae04.
IGNORE_CHATS = [c.strip() for c in os.environ.get("DAVIS_IGNORE_CHATS", "").split(",") if c.strip()]
ONLY_CHATS = [c.strip() for c in os.environ.get("DAVIS_ONLY_CHATS", "").split(",") if c.strip()]

# Write endpoints (settings, approve, pause…) need this token when it is set.
ADMIN_TOKEN = os.environ.get("DAVIS_ADMIN_TOKEN", "")

# Sender names on what Davis queues. Every one starts with "Davis", which is
# how Davis tells its own messages from staff replies.
SENDER = "Davis"
SENDER_CATALOGUE = "Davis (catalogue)"
SENDER_DOCUMENTS = "Davis (documents)"

from .persona import BRAND  # noqa: E402
