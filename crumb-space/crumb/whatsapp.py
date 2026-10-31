"""crumb/whatsapp.py — Crumb on WhatsApp, through the PC bridge.

The bridge (crumb-pc-bridge, the same wa_bridge.py Karlon uses) runs WhatsApp
Web on a PC, reads the most recent chats and POSTs each one here as a
snapshot. New messages from a person are answered by Crumb: the reply is
queued, the bridge picks it up within ~2 s, types it into WhatsApp and acks it.

Bridge endpoints (Authorization: Bearer CRUMB_BRIDGE_TOKEN):
    POST  /api/chats                                  create/update a chat
    POST  /api/chats/{chat_id}/sync                   one chat's snapshot
    PATCH /api/chats/{chat_id}/messages/{id}/wa-ack   delivery result
    GET   /api/outbox/urgent                          Crumb's replies to send
    GET   /api/outbox                                 replies put back for a retry
    GET   /api/outbox/documents                       always empty (no documents)
    POST  /api/chats/{chat_id}/profile-pic            the chat's picture (dashboard)
    POST  /api/chats/{chat_id}/import                 older bridges; accepted, not needed
    POST  /api/chats/{chat_id}/images/import          photos are not kept

When Crumb answers:
    * auto-reply is on (dashboard, and for that chat), and the chat is ready;
    * the newest message in the chat is from the person, and newer than
      CRUMB_WA_REPLY_WINDOW_MIN (a first sync of old chats never sends anything);
    * nobody answered that chat from the phone or the dashboard in the last
      CRUMB_WA_HUMAN_PAUSE_MIN (a person took over: Crumb stays out of it);
    * no reply to that chat is already waiting to go out, and the per-chat
      rate limit and the daily cap allow it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from . import config, guard, knowledge, llm, store
from .wa_clean import chat_slug, clean_chat_name, echo_key, is_system_notice, is_time_only, to_utc_iso

ECHO_WINDOW = timedelta(minutes=30)          # our reply and its WhatsApp echo are the same message
DUPLICATE_WINDOW = timedelta(minutes=10)     # the bridge refuses identical text to two chats within this
HISTORY = 12                                 # messages of the chat Crumb sees

bridge = {"last_seen": None, "last_sync": None, "syncs": 0, "replies_queued": 0}
_tasks: dict[str, asyncio.Task] = {}
_per_chat = guard.RateLimiter(config.WA_REPLIES_PER_CHAT, config.RATE_WINDOW_SEC)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds")


async def require_bridge(authorization: Optional[str] = Header(default=None)) -> None:
    if not config.BRIDGE_TOKEN:
        raise HTTPException(503, "WhatsApp is off: set the CRUMB_BRIDGE_TOKEN secret on the Space")
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(given, config.BRIDGE_TOKEN):
        raise HTTPException(401, "wrong bridge token")
    bridge["last_seen"] = time.time()


router = APIRouter(tags=["whatsapp bridge"], dependencies=[Depends(require_bridge)])


# ── models (what wa_bridge.py sends) ────────────────────────────────────────

class ChatIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    name: str
    wa_name: Optional[str] = None
    avatar_emoji: str = "?"


class SnapshotChat(ChatIn):
    profile_pic_url: Optional[str] = None


class SnapshotMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    external_key: str
    wa_id: Optional[str] = None
    direction: str = "in"
    sender: str = "Unknown"
    kind: str = "text"
    text: Optional[str] = ""
    created_at: str


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    schema_name: str = Field("karlon.chat_snapshot/1", alias="schema")
    chat: SnapshotChat
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    messages: List[SnapshotMessage] = []


# ── chats ───────────────────────────────────────────────────────────────────

def _upsert_chat(conn, chat_id: str, name: str, avatar: str) -> None:
    t = store.now()
    conn.execute(
        "INSERT INTO wa_chats (id, name, wa_name, avatar, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, wa_name=excluded.wa_name, avatar=excluded.avatar, "
        "updated_at=excluded.updated_at", (chat_id, name, name, avatar or "?", t, t))


def _clean_name(chat: ChatIn) -> str:
    return clean_chat_name(chat.name) or clean_chat_name(chat.wa_name)


@router.post("/api/chats")
def upsert_chat(body: ChatIn):
    name = _clean_name(body)
    if not name:
        raise HTTPException(422, f"chat name {body.name!r} has no real name in it")
    with store._lock, store.connect() as conn:
        _upsert_chat(conn, body.id, name, body.avatar_emoji)
    return {"ok": True, "id": body.id}


# ── one chat's snapshot ─────────────────────────────────────────────────────

@router.post("/api/chats/{chat_id}/sync")
async def sync_chat(chat_id: str, body: Snapshot):
    name = _clean_name(body.chat)
    if not name:
        raise HTTPException(422, f"chat name {body.chat.name!r} has no real name in it")
    if chat_slug(name) != chat_id or body.chat.id != chat_id:
        raise HTTPException(422, f"chat id {chat_id} does not match name {name!r} (expected {chat_slug(name)})")
    report = {"chat_id": chat_id, "name": name, "inserted": 0, "updated": 0, "unchanged": 0,
              "linked_app_messages": 0, "dropped_invalid": 0, "pruned": 0, "missing_images": []}
    new_inbound = 0
    with store._lock, store.connect() as conn:
        _upsert_chat(conn, chat_id, name, body.chat.avatar_emoji)
        rows = [dict(r) for r in conn.execute(
            "SELECT id, external_key, wa_msg_id, direction, text, created_at, wa_status, source "
            "FROM wa_messages WHERE chat_id=?", (chat_id,))]
        keys = {r["external_key"] for r in rows if r["external_key"]}
        wa_ids = {r["wa_msg_id"] for r in rows if r["wa_msg_id"]}
        ours = [r for r in rows if r["source"] and not r["wa_msg_id"] and r["wa_status"] in ("sent", "urgent", "pending")]
        seen: set[str] = set()
        for m in body.messages:
            created = to_utc_iso(m.created_at, 0)
            text = (m.text or "").strip()
            if (m.kind == "image" or not created or m.external_key in seen
                    or not text or is_system_notice(text) or is_time_only(text)):
                report["dropped_invalid"] += 1
                continue
            seen.add(m.external_key)
            if m.external_key in keys or (m.wa_id and m.wa_id in wa_ids):
                report["unchanged"] += 1
                continue
            direction = m.direction if m.direction in ("in", "out") else "in"
            if direction == "out":
                # Crumb's own reply, now seen on WhatsApp: link it, don't add it twice.
                when = datetime.fromisoformat(created)
                echo = next((r for r in ours if echo_key(r["text"]) == echo_key(text)
                             and abs(datetime.fromisoformat(r["created_at"]) - when) <= ECHO_WINDOW), None)
                if echo:
                    # WhatsApp's time keeps it in screen order with what the person wrote next.
                    conn.execute("UPDATE wa_messages SET wa_msg_id=?, external_key=?, wa_status='sent', created_at=? "
                                 "WHERE id=?", (m.wa_id, m.external_key, created, echo["id"]))
                    ours.remove(echo)
                    report["linked_app_messages"] += 1
                    continue
            conn.execute(
                "INSERT INTO wa_messages (id, chat_id, direction, sender, text, created_at, external_key, wa_msg_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), chat_id, direction,
                 "You (WhatsApp)" if direction == "out" else ((m.sender or "").strip() or name),
                 text, created, m.external_key, m.wa_id))
            report["inserted"] += 1
            new_inbound += direction == "in"
        pic = conn.execute("SELECT version FROM wa_pics WHERE chat_id=?", (chat_id,)).fetchone()
    report["profile_pic_version"] = pic["version"] if pic else None
    bridge["last_sync"] = time.time()
    bridge["syncs"] += 1
    if new_inbound:
        store.event("wa_in", chat=name, messages=new_inbound)
        schedule(chat_id)
    return report


# ── deciding and writing the reply ─────────────────────────────────────────

def schedule(chat_id: str) -> None:
    """Answer the chat after a short pause; a newer message restarts the pause."""
    old = _tasks.get(chat_id)
    if old and not old.done():
        old.cancel()
    _tasks[chat_id] = asyncio.get_running_loop().create_task(_debounced(chat_id))


async def _debounced(chat_id: str) -> None:
    try:
        await asyncio.sleep(config.WA_DEBOUNCE_SEC)
        await answer(chat_id)
    except asyncio.CancelledError:
        pass
    except Exception as e:                       # never lose the loop over one chat
        store.event("error", where="whatsapp", chat=chat_id, error=f"{type(e).__name__}: {e}"[:200])


def _why_not(chat_id: str) -> tuple[Optional[str], Optional[dict], list[dict]]:
    """(reason to stay quiet or None, the chat, its recent messages oldest first)."""
    s = store.settings()
    with store.connect() as conn:
        chat = conn.execute("SELECT * FROM wa_chats WHERE id=?", (chat_id,)).fetchone()
        msgs = [dict(r) for r in conn.execute(
            "SELECT * FROM wa_messages WHERE chat_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (chat_id, HISTORY))][::-1]
    if not chat:
        return "unknown chat", None, msgs
    chat = dict(chat)
    if not s.get("wa_auto_reply") or not s.get("ready"):
        return "auto-reply is off", chat, msgs
    if not chat["auto_reply"]:
        return "auto-reply is off for this chat", chat, msgs
    if not msgs or msgs[-1]["direction"] != "in":
        return "nothing new from the person", chat, msgs
    if any(m["direction"] == "out" and m["wa_status"] in ("urgent", "pending") for m in msgs):
        return "a reply is already waiting to go out", chat, msgs
    last_in = datetime.fromisoformat(msgs[-1]["created_at"])
    if _now() - last_in > timedelta(minutes=config.WA_REPLY_WINDOW_MIN):
        return "the message is too old", chat, msgs
    pause = timedelta(minutes=config.WA_HUMAN_PAUSE_MIN)
    if config.WA_HUMAN_PAUSE_MIN and any(
            m["direction"] == "out" and m["source"] in (None, "dashboard") and last_in - datetime.fromisoformat(m["created_at"]) <= pause
            for m in msgs):
        return "someone is answering from the phone", chat, msgs
    return None, chat, msgs


def _whatsapp_text(text: str) -> str:
    return text.replace("**", "*").strip()


def _avoid_duplicate(conn, chat: dict, text: str) -> str:
    """The bridge refuses the same text to two chats within 10 minutes (its
    guard against bulk messages). Two people asking the same thing is normal:
    greet this one by name so the answer is theirs."""
    since = _iso(_now() - DUPLICATE_WINDOW)
    dup = conn.execute("SELECT 1 FROM wa_messages WHERE direction='out' AND source IS NOT NULL AND chat_id != ? "
                       "AND text = ? AND created_at >= ? LIMIT 1", (chat["id"], text, since)).fetchone()
    if not dup:
        return text
    first = (chat["name"] or "").split()[0] if chat["name"] else ""
    return f"Hi {first}! {text}" if first.isalpha() else f"Hello again! {text}"


async def answer(chat_id: str) -> Optional[dict]:
    reason, chat, msgs = _why_not(chat_id)
    if reason:
        return {"queued": False, "reason": reason}
    if not _per_chat.allow(chat_id):
        store.event("rate_limited", conversation=f"wa:{chat['name']}")
        return {"queued": False, "reason": "too many replies to this chat"}
    history = llm.clean_history([{"role": "user" if m["direction"] == "in" else "assistant", "content": m["text"]}
                                 for m in msgs])
    question = history[-1]["content"] if history else ""
    notes = store.settings().get("notes") or ""
    if llm.available() and not store.take_daily():
        result = llm.Reply(knowledge.offline_answer(question), "offline", error="daily_limit")
    else:
        result = await llm.reply(history, notes, channel="whatsapp")
    # The person may have written again, or someone answered from the phone,
    # while Crumb was thinking: check again before queueing.
    reason, chat, _ = _why_not(chat_id)
    if reason:
        return {"queued": False, "reason": reason}
    msg_id = str(uuid.uuid4())
    with store._lock, store.connect() as conn:
        text = _avoid_duplicate(conn, chat, _whatsapp_text(result.text))
        conn.execute(
            "INSERT INTO wa_messages (id, chat_id, direction, sender, text, created_at, wa_status, source, latency_ms) "
            "VALUES (?, ?, 'out', ?, ?, ?, 'urgent', ?, ?)",
            (msg_id, chat_id, knowledge.NAME, text, _iso(_now()), result.source, result.latency_ms))
    bridge["replies_queued"] += 1
    store.event("error" if result.error and result.error != "no_api_key" else "wa_reply",
                chat=chat["name"], source=result.source, ms=result.latency_ms, error=result.error)
    return {"queued": True, "id": msg_id, "text": text, "source": result.source}


def queue_manual(chat_id: str, text: str) -> dict:
    """A message typed on the dashboard, sent through the bridge as is."""
    with store._lock, store.connect() as conn:
        if not conn.execute("SELECT 1 FROM wa_chats WHERE id=?", (chat_id,)).fetchone():
            raise HTTPException(404, "chat not found")
        msg_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO wa_messages (id, chat_id, direction, sender, text, created_at, wa_status, source) "
            "VALUES (?, ?, 'out', 'Dashboard', ?, ?, 'urgent', 'dashboard')", (msg_id, chat_id, text, _iso(_now())))
    store.event("wa_manual", chat=chat_id)
    return {"queued": True, "id": msg_id}


# ── the outbox the bridge delivers ─────────────────────────────────────────

def _outbox(status: str) -> list[dict]:
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT m.id, m.chat_id, m.sender, m.text, 'text' AS kind, NULL AS media_url, m.created_at, "
            "m.direction, m.wa_status, c.wa_name, c.name AS chat_name "
            "FROM wa_messages m JOIN wa_chats c ON c.id = m.chat_id "
            "WHERE m.direction='out' AND m.wa_status=? ORDER BY m.created_at", (status,)).fetchall()
    return [dict(r) for r in rows]


@router.get("/api/outbox/urgent")
def outbox_urgent():
    return _outbox("urgent")


@router.get("/api/outbox")
def outbox_pending():
    return _outbox("pending")


@router.get("/api/outbox/documents")
def outbox_documents():
    return []


@router.patch("/api/chats/{chat_id}/messages/{msg_id}/wa-ack")
def ack(chat_id: str, msg_id: str,
        status: str = Query("sent", pattern="^(sent|error|pending|urgent|documents|superseded)$")):
    with store._lock, store.connect() as conn:
        cur = conn.execute("UPDATE wa_messages SET wa_status=? WHERE id=? AND chat_id=?", (status, msg_id, chat_id))
    if not cur.rowcount:
        raise HTTPException(404, "message not found")
    if status == "error":
        store.event("error", where="whatsapp delivery", chat=chat_id)
    return {"ok": True, "status": status}


# ── extras the bridge sends ─────────────────────────────────────────────────

@router.post("/api/chats/{chat_id}/profile-pic")
async def profile_pic(chat_id: str, file: UploadFile = File(...)):
    data = await file.read()
    if not data or len(data) > 512_000:
        raise HTTPException(400, "picture missing or too big")
    with store._lock, store.connect() as conn:
        if not conn.execute("SELECT 1 FROM wa_chats WHERE id=?", (chat_id,)).fetchone():
            raise HTTPException(404, "chat not found")
        version = hashlib.sha1(data).hexdigest()[:12]
        conn.execute("INSERT INTO wa_pics (chat_id, version, data) VALUES (?, ?, ?) ON CONFLICT(chat_id) "
                     "DO UPDATE SET version=excluded.version, data=excluded.data", (chat_id, version, data))
    return {"ok": True, "version": version}


@router.post("/api/chats/{chat_id}/import")
def legacy_import(chat_id: str, body: dict):
    return {"imported": 0, "skipped": len(body.get("messages") or []), "note": "use /sync"}


@router.post("/api/chats/{chat_id}/images/import")
def images_import(chat_id: str):
    return {"ok": True, "stored": False}


# ── for the dashboard ───────────────────────────────────────────────────────

def status() -> dict:
    with store.connect() as conn:
        one = lambda q, *a: conn.execute(q, a).fetchone()[0]  # noqa: E731
        counts = {
            "chats": one("SELECT COUNT(*) FROM wa_chats"),
            "received": one("SELECT COUNT(*) FROM wa_messages WHERE direction='in'"),
            "crumb_replies": one("SELECT COUNT(*) FROM wa_messages WHERE source IN ('model','offline','refusal')"),
            "waiting": one("SELECT COUNT(*) FROM wa_messages WHERE wa_status IN ('urgent','pending')"),
            "failed": one("SELECT COUNT(*) FROM wa_messages WHERE wa_status='error'"),
        }
    seen = bridge["last_seen"]
    return {"configured": bool(config.BRIDGE_TOKEN), "auto_reply": bool(store.settings().get("wa_auto_reply")),
            "bridge_last_seen_s": int(time.time() - seen) if seen else None,
            "bridge_online": bool(seen and time.time() - seen < 90),
            "last_sync_s": int(time.time() - bridge["last_sync"]) if bridge["last_sync"] else None,
            "syncs": bridge["syncs"], **counts,
            "rules": {"reply_window_min": config.WA_REPLY_WINDOW_MIN, "human_pause_min": config.WA_HUMAN_PAUSE_MIN,
                      "debounce_s": config.WA_DEBOUNCE_SEC, "replies_per_chat": config.WA_REPLIES_PER_CHAT}}


def chats(limit: int = 100) -> list[dict]:
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT c.*, (SELECT version FROM wa_pics p WHERE p.chat_id=c.id) AS pic, "
            "(SELECT text FROM wa_messages m WHERE m.chat_id=c.id ORDER BY created_at DESC, rowid DESC LIMIT 1) AS last_text, "
            "(SELECT direction FROM wa_messages m WHERE m.chat_id=c.id ORDER BY created_at DESC, rowid DESC LIMIT 1) AS last_dir, "
            "(SELECT created_at FROM wa_messages m WHERE m.chat_id=c.id ORDER BY created_at DESC, rowid DESC LIMIT 1) AS last_at "
            "FROM wa_chats c ORDER BY COALESCE(last_at, c.updated_at) DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def chat(chat_id: str) -> Optional[dict]:
    with store.connect() as conn:
        c = conn.execute("SELECT * FROM wa_chats WHERE id=?", (chat_id,)).fetchone()
        if not c:
            return None
        msgs = conn.execute("SELECT * FROM wa_messages WHERE chat_id=? ORDER BY created_at DESC, rowid DESC LIMIT 200",
                            (chat_id,)).fetchall()
    out = {**dict(c), "messages": [dict(m) for m in msgs][::-1]}
    out["why_quiet"] = _why_not(chat_id)[0]
    return out


def set_auto_reply(chat_id: str, on: bool) -> dict:
    with store._lock, store.connect() as conn:
        cur = conn.execute("UPDATE wa_chats SET auto_reply=? WHERE id=?", (int(on), chat_id))
    if not cur.rowcount:
        raise HTTPException(404, "chat not found")
    return {"ok": True, "auto_reply": on}


def picture(chat_id: str) -> Response:
    with store.connect() as conn:
        r = conn.execute("SELECT data FROM wa_pics WHERE chat_id=?", (chat_id,)).fetchone()
    if not r:
        raise HTTPException(404, "no picture")
    return Response(r["data"], media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})
