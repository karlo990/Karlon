"""davis/config.py — Karl's knobs (the assistant was called Davis; the
package and the /api/davis URLs keep that name so nothing that calls them breaks). Env vars give the defaults; the dashboard
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
PROVIDER = (os.environ.get("KARL_PROVIDER") or os.environ.get("DAVIS_PROVIDER") or "local").strip().lower()
if PROVIDER not in PROVIDERS:
    PROVIDER = "local"

# Karl's mind (mind.py) on the local model: STATE reads the chat into the
# booking state, REPLY writes the message. Both share one prompt prefix.
#   auto   = STATE runs only when the rules can't account for the turn (mind.needs_model)
#   always = STATE on every turn;  off = rules only (the model still writes replies)
KARL_UNDERSTAND = os.environ.get("KARL_UNDERSTAND", "auto") if os.environ.get("KARL_UNDERSTAND", "auto") in (
    "auto", "always", "off") else "auto"
KARL_STATE_MAX_TOKENS = _int("KARL_STATE_MAX_TOKENS", 300, 120)
KARL_REPLY_MAX_TOKENS = _int("KARL_REPLY_MAX_TOKENS", 140, 60)
# Who writes the reply on the local model (generation is ~3 tokens/s on 2 cores):
#   auto   = the model only when the guest asked something the plan's own wording
#            doesn't answer (open questions); booking steps use the plan's wording
#   always = the model writes every reply
KARL_REPLY = os.environ.get("KARL_REPLY", "auto") if os.environ.get("KARL_REPLY", "auto") in ("auto", "always") \
    else "auto"
KARL_TRANSCRIPT = _int("KARL_TRANSCRIPT", 24, 6)            # messages of the chat Karl reads every turn
KARL_LINE_CHARS = _int("KARL_LINE_CHARS", 300, 80)          # longest message line in the transcript
KARL_MAX_WORDS = _int("KARL_MAX_WORDS", 50, 20)             # a model reply may answer a few questions at once
LOCAL_CACHE_MB = _int("DAVIS_LOCAL_CACHE_MB", 1024, 0)      # RAM cache of evaluated prompt prefixes (0 = off)

# The local model. Default: Qwen3-4B-Instruct-2507, 4-bit Q4_K_M (2.5 GB file,
# ~4.5 GB RAM with its context and the prefix cache; Apache-2.0). Chosen by
# tests/eval_state.py on the October chats at 2 threads: the most accurate
# of Qwen2.5-1.5B, Qwen2.5-3B, Phi-4-mini and Qwen3-4B (numbers in
# .claude/skills/karl-local-llm/SKILL.md). The Dockerfile downloads the file
# at build time; if it is missing at start it is downloaded once.
LOCAL_MODEL_REPO = os.environ.get("DAVIS_LOCAL_MODEL_REPO", "unsloth/Qwen3-4B-Instruct-2507-GGUF")
LOCAL_MODEL_FILE = os.environ.get("DAVIS_LOCAL_MODEL_FILE", "Qwen3-4B-Instruct-2507-Q4_K_M.gguf")
LOCAL_MODEL_DIR = os.environ.get("DAVIS_LOCAL_MODEL_DIR",
                                 "/models" if os.path.isdir("/models") else
                                 os.path.join(os.path.dirname(__file__), "..", "..", "models"))
LOCAL_THREADS = _int("DAVIS_LOCAL_THREADS", 0)          # 0 = the cores this container really has
LOCAL_CTX = _int("DAVIS_LOCAL_CTX", 4096, 1024)
LOCAL_MAX_TOKENS = _int("DAVIS_LOCAL_MAX_TOKENS", 220, 40)   # default cap; Karl's steps pass their own
LOCAL_TEMPERATURE = _float("DAVIS_LOCAL_TEMPERATURE", 0.3)
LOCAL_PRELOAD = os.environ.get("DAVIS_LOCAL_PRELOAD", "1") not in ("0", "false", "no")
LLM_TIMEOUT_SEC = _float("DAVIS_LLM_TIMEOUT_SEC", 120.0, 3.0)  # a 4B STATE step on 2 cores can take ~60 s

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
IMAGES_PER_PHOTO_REQUEST = _int("DAVIS_IMAGES_PER_PHOTO_REQUEST", 8, 1)   # "send pics": more than the card had

# ── when Davis stays quiet ─────────────────────────────────────────────────
DEBOUNCE_SEC = _float("DAVIS_DEBOUNCE_SEC", 5.0)            # let the guest finish typing (speed to lead)
FRESH_MINUTES = _int("DAVIS_FRESH_MINUTES", 3 * 24 * 60, 1)  # answer guests who wrote in the last 3 days
# 0 = Davis answers every guest message, even right after a staff reply (Davis
# always has the last word; "Take over" on the dashboard mutes a chat).
HUMAN_QUIET_MINUTES = _int("DAVIS_HUMAN_QUIET_MINUTES", 0)
HISTORY_MESSAGES = _int("DAVIS_HISTORY_MESSAGES", 24, 4)    # what Davis reads per turn
# Where the model writes: "open" = only answers to open questions (from the
# team's house notes); every booking step uses the persona's lines (instant,
# exact). "all" = the model rewrites every reply too (slower; each rewrite
# must still pass the guard). Changeable on the dashboard.
#   ai   = the model writes every reply itself. If it can't write a safe one
#          it tries again shortly (AI_RETRIES), and only then sends the plan's
#          own wording: a guest is never left waiting for a person. Default.
LLM_FOR = os.environ.get("DAVIS_LLM_FOR", "ai") if os.environ.get("DAVIS_LLM_FOR", "ai") in ("open", "all", "ai") \
    else "ai"
MODEL_HISTORY = _int("DAVIS_MODEL_HISTORY", 10, 2)          # of which the model sees the last N

# ── no human in the middle: Davis resolves everything itself ───────────────
AI_RETRIES = _int("DAVIS_AI_RETRIES", 2)                    # model retries before the plan's wording
AI_RETRY_SEC = _int("DAVIS_AI_RETRY_SEC", 40, 5)
REFRESH_TRIES = _int("DAVIS_REFRESH_TRIES", 2, 1)           # live-price requests before offering other dates
REFRESH_TIMEOUT_MINUTES = _int("DAVIS_REFRESH_TIMEOUT_MINUTES", 12, 3)
# Houses go to the guest as soon as this many are priced for their dates
# (the scraper keeps adding more; "more options" sends them).
EARLY_PRESENT = _int("DAVIS_EARLY_PRESENT", 3, 1)
# Customer care: one friendly nudge when a guest goes quiet mid-booking
# (houses sent, details or payment pending). 0 = off.
FOLLOWUP_HOURS = _float("DAVIS_FOLLOWUP_HOURS", 3.0)
FOLLOWUP_MAX = _int("DAVIS_FOLLOWUP_MAX", 1)                # nudges per guest silence
FOLLOWUP_WINDOW_HOURS = _int("DAVIS_FOLLOWUP_WINDOW_HOURS", 23, 1)   # never nudge a long-gone chat

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
# how Karl tells its own messages from staff replies ("Front Desk", the
# owner's phone). Messages from before the rename say "Davis": still Karl's.
NAME = "Karl"
SENDER = "Karl"
SENDER_CATALOGUE = "Karl (catalogue)"
SENDER_DOCUMENTS = "Karl (documents)"

from .persona import BRAND  # noqa: E402
