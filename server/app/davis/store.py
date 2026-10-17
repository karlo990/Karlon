"""davis/store.py — Davis's tables: settings, one session per chat (the
booking frame = the conversation's memory), an event log, drafts waiting
for approval in shadow mode, and catalogue flags (hide / feature a house)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from ..database import get_db
from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS davis_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS davis_sessions (
    chat_id         TEXT PRIMARY KEY,
    stage           TEXT NOT NULL DEFAULT 'new',
    slots           TEXT NOT NULL DEFAULT '{}',
    choice          TEXT,                 -- the option the guest picked (JSON)
    last_inbound_id TEXT,                 -- newest guest message already handled
    last_reply_at   TEXT,
    paused          INTEGER NOT NULL DEFAULT 0,
    handoff         INTEGER NOT NULL DEFAULT 0,   -- staff took over on the dashboard: Davis is muted
    handoff_reason  TEXT,
    attention       INTEGER NOT NULL DEFAULT 0,   -- Davis alerted the team (Davis keeps replying)
    attention_reason TEXT,
    opted_out       INTEGER NOT NULL DEFAULT 0,
    refresh_job_id  INTEGER,              -- waiting for live prices for the guest's dates
    invoice_id      TEXT,
    invoice_key     TEXT,                 -- listing|dates|name the invoice was made for
    terms_sent_at   TEXT,
    replies         INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS davis_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    TEXT,
    ts         TEXT NOT NULL,
    kind       TEXT NOT NULL,
    detail     TEXT,
    latency_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_davis_events_ts ON davis_events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_davis_events_chat ON davis_events(chat_id, ts DESC);
CREATE TABLE IF NOT EXISTS davis_drafts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL,
    inbound_id  TEXT,
    created_at  TEXT NOT NULL,
    plan        TEXT NOT NULL,            -- actions + the session changes they make
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | discarded | superseded
    decided_at  TEXT,
    decided_by  TEXT
);
CREATE INDEX IF NOT EXISTS idx_davis_drafts_status ON davis_drafts(status, created_at DESC);
CREATE TABLE IF NOT EXISTS davis_catalogue (
    listing_url TEXT PRIMARY KEY,
    hidden      INTEGER NOT NULL DEFAULT 0,
    featured    INTEGER NOT NULL DEFAULT 0,
    note        TEXT,
    updated_at  TEXT
);
"""

SETTING_DEFAULTS: dict[str, Any] = {
    "mode": config.MODE,
    "provider": config.PROVIDER,
    "model": config.OLLAMA_MODEL,  # the model name for the ollama provider (local uses DAVIS_LOCAL_MODEL_*)
    "paused": False,              # everything stops, all chats
    "only_chats": config.ONLY_CHATS,      # non-empty = Davis answers only these chat ids (safe rollout)
    "ignore_chats": config.IGNORE_CHATS,  # Davis never answers these (team, family, suppliers)
    "llm_for": config.LLM_FOR,    # open = model only answers open questions; all = rewrites every reply
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def init(conn) -> None:
    conn.executescript(SCHEMA)
    for stmt in ("ALTER TABLE davis_sessions ADD COLUMN attention INTEGER NOT NULL DEFAULT 0",
                 "ALTER TABLE davis_sessions ADD COLUMN attention_reason TEXT"):
        try:
            conn.execute(stmt)
        except Exception:
            pass                       # column already there
    conn.commit()


# ─────────────────────────── settings ────────────────────────────────────────

def get_settings(conn=None) -> dict:
    own = conn is None
    conn = conn or get_db()
    try:
        out = dict(SETTING_DEFAULTS)
        for r in conn.execute("SELECT key, value FROM davis_settings"):
            if r["key"] in out:
                try:
                    out[r["key"]] = json.loads(r["value"])
                except Exception:
                    pass
        # The Space variable always applies, on top of what the dashboard added.
        out["ignore_chats"] = sorted(set(out.get("ignore_chats") or []) | set(config.IGNORE_CHATS))
        if out["provider"] not in config.PROVIDERS:      # e.g. a provider that was removed
            out["provider"] = config.PROVIDER
        return out
    finally:
        if own:
            conn.close()


def put_settings(changes: dict) -> dict:
    conn = get_db()
    try:
        for k, v in changes.items():
            if k in SETTING_DEFAULTS:
                conn.execute(
                    "INSERT INTO davis_settings (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (k, json.dumps(v), now_iso()))
        conn.commit()
        return get_settings(conn)
    finally:
        conn.close()


# ─────────────────────────── sessions ────────────────────────────────────────

_JSON_COLS = ("slots", "choice")


def _session_row(row) -> dict:
    d = dict(row)
    for c in _JSON_COLS:
        try:
            d[c] = json.loads(d[c]) if d.get(c) else ({} if c == "slots" else None)
        except Exception:
            d[c] = {} if c == "slots" else None
    for c in ("paused", "handoff", "opted_out", "attention"):
        d[c] = bool(d.get(c))
    return d


def get_session(conn, chat_id: str) -> dict:
    row = conn.execute("SELECT * FROM davis_sessions WHERE chat_id=?", (chat_id,)).fetchone()
    if row:
        return _session_row(row)
    now = now_iso()
    conn.execute("INSERT OR IGNORE INTO davis_sessions (chat_id, created_at, updated_at) VALUES (?, ?, ?)",
                 (chat_id, now, now))
    conn.commit()
    return _session_row(conn.execute("SELECT * FROM davis_sessions WHERE chat_id=?", (chat_id,)).fetchone())


def update_session(conn, chat_id: str, **fields) -> None:
    if not fields:
        return
    vals = []
    for k, v in fields.items():
        vals.append(json.dumps(v) if k in _JSON_COLS and v is not None else
                    (int(v) if isinstance(v, bool) else v))
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE davis_sessions SET {sets}, updated_at=? WHERE chat_id=?", (*vals, now_iso(), chat_id))
    conn.commit()


def claim_inbound(conn, chat_id: str, inbound_id: str) -> bool:
    """Mark this guest message as being handled. Atomic, so two passes (two
    hooks, the sweeper, a second server process) never answer it twice."""
    cur = conn.execute(
        "UPDATE davis_sessions SET last_inbound_id=?, updated_at=? "
        "WHERE chat_id=? AND (last_inbound_id IS NULL OR last_inbound_id != ?)",
        (inbound_id, now_iso(), chat_id, inbound_id))
    conn.commit()
    return cur.rowcount == 1


# ─────────────────────────── events ──────────────────────────────────────────

def log_event(conn, chat_id: Optional[str], kind: str, detail: Any = None,
              latency_ms: Optional[int] = None) -> dict:
    ev = {"chat_id": chat_id, "ts": now_iso(), "kind": kind, "detail": detail, "latency_ms": latency_ms}
    cur = conn.execute("INSERT INTO davis_events (chat_id, ts, kind, detail, latency_ms) VALUES (?, ?, ?, ?, ?)",
                       (chat_id, ev["ts"], kind, json.dumps(detail, default=str), latency_ms))
    conn.commit()
    ev["id"] = cur.lastrowid
    return ev


def events(conn, chat_id: Optional[str] = None, limit: int = 100) -> list[dict]:
    q = "SELECT * FROM davis_events" + (" WHERE chat_id=?" if chat_id else "") + " ORDER BY id DESC LIMIT ?"
    rows = conn.execute(q, ((chat_id, limit) if chat_id else (limit,))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["detail"] = json.loads(d["detail"]) if d["detail"] else None
        except Exception:
            pass
        out.append(d)
    return out


# ─────────────────────────── drafts ──────────────────────────────────────────

def add_draft(conn, chat_id: str, inbound_id: Optional[str], plan: dict) -> int:
    conn.execute("UPDATE davis_drafts SET status='superseded', decided_at=? WHERE chat_id=? AND status='pending'",
                 (now_iso(), chat_id))
    cur = conn.execute("INSERT INTO davis_drafts (chat_id, inbound_id, created_at, plan) VALUES (?, ?, ?, ?)",
                       (chat_id, inbound_id, now_iso(), json.dumps(plan, default=str)))
    conn.commit()
    return cur.lastrowid


def draft(conn, draft_id: int) -> Optional[dict]:
    r = conn.execute("SELECT * FROM davis_drafts WHERE id=?", (draft_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["plan"] = json.loads(d["plan"])
    return d


def drafts(conn, status: Optional[str] = "pending", limit: int = 50) -> list[dict]:
    q = "SELECT d.*, c.name AS chat_name FROM davis_drafts d LEFT JOIN chats c ON c.id=d.chat_id"
    args: tuple = ()
    if status:
        q += " WHERE d.status=?"
        args = (status,)
    rows = conn.execute(q + " ORDER BY d.id DESC LIMIT ?", (*args, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["plan"] = json.loads(d["plan"])
        out.append(d)
    return out


def decide_draft(conn, draft_id: int, status: str, by: str = "dashboard") -> bool:
    cur = conn.execute("UPDATE davis_drafts SET status=?, decided_at=?, decided_by=? WHERE id=? AND status='pending'",
                       (status, now_iso(), by, draft_id))
    conn.commit()
    return cur.rowcount == 1


# ─────────────────────────── catalogue flags ─────────────────────────────────

def catalogue_flags(conn) -> dict[str, dict]:
    return {r["listing_url"]: dict(r) for r in conn.execute("SELECT * FROM davis_catalogue")}


def set_catalogue_flag(conn, listing_url: str, **fields) -> dict:
    conn.execute("INSERT OR IGNORE INTO davis_catalogue (listing_url, updated_at) VALUES (?, ?)",
                 (listing_url, now_iso()))
    for k in ("hidden", "featured", "note"):
        if k in fields and fields[k] is not None:
            v = int(bool(fields[k])) if k != "note" else str(fields[k])[:300]
            conn.execute(f"UPDATE davis_catalogue SET {k}=?, updated_at=? WHERE listing_url=?",
                         (v, now_iso(), listing_url))
    conn.commit()
    return dict(conn.execute("SELECT * FROM davis_catalogue WHERE listing_url=?", (listing_url,)).fetchone())
