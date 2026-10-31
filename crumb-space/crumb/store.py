"""crumb/store.py — SQLite: conversations, messages, events (the dashboard's
feed), feedback, settings changed on the dashboard, and the daily counter.

A free Space has no persistent disk, so this resets when the Space restarts;
settings that must survive go in knowledge.py / the Space variables."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from . import config, knowledge

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,          -- the chat session id (from the cookie, or the IP's hash)
    started_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    turns       INTEGER NOT NULL DEFAULT 0,
    country     TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role            TEXT NOT NULL,         -- user | assistant
    text            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    source          TEXT,                  -- model | offline | refusal | error
    latency_ms      INTEGER,
    input_tokens    INTEGER,
    output_tokens   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,                 -- session_ok | session_failed | chat | rate_limited | error | settings
    detail  TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    conversation_id TEXT,
    rating          TEXT NOT NULL,         -- up | down
    comment         TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily (
    day   TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0
);
-- WhatsApp, through the PC bridge (crumb/whatsapp.py)
CREATE TABLE IF NOT EXISTS wa_chats (
    id          TEXT PRIMARY KEY,          -- chat_slug(name), the same id the bridge computes
    name        TEXT NOT NULL,
    wa_name     TEXT NOT NULL,             -- the name the bridge clicks in WhatsApp Web
    avatar      TEXT,
    auto_reply  INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wa_messages (
    id           TEXT PRIMARY KEY,
    chat_id      TEXT NOT NULL,
    direction    TEXT NOT NULL,            -- in | out
    sender       TEXT,
    text         TEXT NOT NULL,
    created_at   TEXT NOT NULL,            -- UTC ISO
    external_key TEXT,                     -- the bridge's key for a message it read
    wa_msg_id    TEXT,                     -- WhatsApp's own id, once seen on WhatsApp
    wa_status    TEXT,                     -- written here: urgent | pending | sent | error; read from WhatsApp: NULL
    source       TEXT,                     -- model | offline | refusal | dashboard (written here), NULL (read)
    latency_ms   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_wa_messages_chat ON wa_messages(chat_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wa_messages_key ON wa_messages(chat_id, external_key);
CREATE TABLE IF NOT EXISTS wa_pics (
    chat_id  TEXT PRIMARY KEY,
    version  TEXT NOT NULL,
    data     BLOB NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock, connect() as conn:
        conn.executescript(SCHEMA)


def event(kind: str, **detail: Any) -> None:
    with _lock, connect() as conn:
        conn.execute("INSERT INTO events (ts, kind, detail) VALUES (?, ?, ?)", (now(), kind, json.dumps(detail)))
        conn.execute("DELETE FROM events WHERE id < (SELECT MAX(id) - 2000 FROM events)")


def events(limit: int = 60) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{**dict(r), "detail": json.loads(r["detail"] or "{}")} for r in rows]


# ── settings ────────────────────────────────────────────────────────────────

def settings() -> dict:
    out = json.loads(json.dumps(knowledge.DEFAULT_SETTINGS))
    with connect() as conn:
        for r in conn.execute("SELECT key, value FROM settings"):
            if r["key"] in out:
                out[r["key"]] = json.loads(r["value"])
    return out


def put_settings(changes: dict) -> dict:
    with _lock, connect() as conn:
        for k, v in changes.items():
            if k in knowledge.DEFAULT_SETTINGS:
                conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, json.dumps(v)))
    return settings()


# ── conversations ───────────────────────────────────────────────────────────

def log_turn(conversation_id: str, question: str, answer: str, source: str, latency_ms: int,
             input_tokens: Optional[int] = None, output_tokens: Optional[int] = None) -> None:
    t = now()
    with _lock, connect() as conn:
        conn.execute("INSERT INTO conversations (id, started_at, updated_at, turns) VALUES (?, ?, ?, 1) "
                     "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at, turns=turns+1",
                     (conversation_id, t, t))
        conn.execute("INSERT INTO messages (conversation_id, role, text, created_at) VALUES (?, 'user', ?, ?)",
                     (conversation_id, question, t))
        conn.execute("INSERT INTO messages (conversation_id, role, text, created_at, source, latency_ms, "
                     "input_tokens, output_tokens) VALUES (?, 'assistant', ?, ?, ?, ?, ?, ?)",
                     (conversation_id, answer, t, source, latency_ms, input_tokens, output_tokens))


def conversations(limit: int = 50) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT c.*, (SELECT text FROM messages m WHERE m.conversation_id=c.id AND m.role='user' "
            "ORDER BY m.id DESC LIMIT 1) AS last_question FROM conversations c ORDER BY updated_at DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


def conversation(conversation_id: str) -> Optional[dict]:
    with connect() as conn:
        c = conn.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if not c:
            return None
        msgs = conn.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (conversation_id,)).fetchall()
    return {**dict(c), "messages": [dict(m) for m in msgs]}


def add_feedback(conversation_id: Optional[str], rating: str, comment: str) -> int:
    with _lock, connect() as conn:
        cur = conn.execute("INSERT INTO feedback (created_at, conversation_id, rating, comment) VALUES (?, ?, ?, ?)",
                           (now(), conversation_id, rating, comment))
        return cur.lastrowid


def feedback(limit: int = 50) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM feedback ORDER BY id DESC LIMIT ?", (limit,))]


# ── the daily cap ───────────────────────────────────────────────────────────

def take_daily() -> bool:
    """Count one model reply against today's cap. False when the cap is reached."""
    day = datetime.now(timezone.utc).date().isoformat()
    with _lock, connect() as conn:
        row = conn.execute("SELECT count FROM daily WHERE day=?", (day,)).fetchone()
        if row and row["count"] >= config.DAILY_LIMIT:
            return False
        conn.execute("INSERT INTO daily (day, count) VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET count=count+1",
                     (day,))
    return True


def stats() -> dict:
    day = datetime.now(timezone.utc).date().isoformat()
    with connect() as conn:
        one = lambda q, *a: conn.execute(q, a).fetchone()[0]  # noqa: E731
        lat = [r[0] for r in conn.execute(
            "SELECT latency_ms FROM messages WHERE role='assistant' AND source='model' AND latency_ms IS NOT NULL "
            "ORDER BY id DESC LIMIT 500")]
        lat.sort()
        return {
            "conversations": one("SELECT COUNT(*) FROM conversations"),
            "questions": one("SELECT COUNT(*) FROM messages WHERE role='user'"),
            "today": (conn.execute("SELECT count FROM daily WHERE day=?", (day,)).fetchone() or [0])[0],
            "by_source": {r["source"]: r["n"] for r in conn.execute(
                "SELECT source, COUNT(*) AS n FROM messages WHERE role='assistant' GROUP BY source")},
            "sessions_ok": one("SELECT COUNT(*) FROM events WHERE kind='session_ok'"),
            "sessions_failed": one("SELECT COUNT(*) FROM events WHERE kind='session_failed'"),
            "rate_limited": one("SELECT COUNT(*) FROM events WHERE kind='rate_limited'"),
            "errors": one("SELECT COUNT(*) FROM events WHERE kind='error'"),
            "latency_ms_p50": lat[len(lat) // 2] if lat else None,
            "latency_ms_p95": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None,
            "tokens_in": one("SELECT COALESCE(SUM(input_tokens),0) FROM messages"),
            "tokens_out": one("SELECT COALESCE(SUM(output_tokens),0) FROM messages"),
            "feedback_up": one("SELECT COUNT(*) FROM feedback WHERE rating='up'"),
            "feedback_down": one("SELECT COUNT(*) FROM feedback WHERE rating='down'"),
        }
