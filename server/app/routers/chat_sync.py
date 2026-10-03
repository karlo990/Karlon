"""routers/chat_sync.py — apply one chat's WhatsApp snapshot atomically.

POST /api/chats/{chat_id}/sync
    Body: the JSON snapshot wa_bridge builds for ONE chat (schema
    "karlon.chat_snapshot/1": the chat, its messages in WhatsApp's on-screen
    order with UTC times, and the time window they cover). Applied in a single
    transaction, so a chat is never left half-updated:

      1. The chat is upserted under its CLEAN name (wa_clean.clean_chat_name),
         and any "alias" chats older bridges created for the same person
         ("1 unread message\\nKarl", "2 unread messages\\nKarl", ...) are
         merged into it and deleted.
      2. Each message is matched to an existing row by external_key or
         WhatsApp message id. Bridge-imported rows are corrected in place
         (time, side, sender, text); rows composed in the app (wa_status set)
         are never rewritten. An outgoing message that is the WhatsApp echo of
         something sent from the app is linked to that row, not duplicated.
      3. New messages are inserted. New photos are NOT (they need bytes): their
         keys come back in `missing_images` and the bridge uploads just those.
      4. Prune: bridge-imported rows inside the snapshot's time window that the
         snapshot no longer contains (system notices, duplicates, rows stamped
         with the import time) are deleted — capped by a safety limit so a bad
         scan can't wipe a chat (override with ?force=true after checking the
         saved snapshot JSON).
      5. Every remaining timestamp in the chat is normalised to UTC, so the
         whole thread sorts correctly.

POST /api/chats/repair-names[?apply=true]
    One-off: merges every alias chat into its clean-named chat across the
    whole database. Dry run by default.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ..database import get_db
from ..models import ChatSnapshotIn
from ..wa_clean import (
    chat_slug, clean_chat_name, is_system_notice, is_time_only, echo_key, to_utc_iso,
)
from ..ws_manager import manager
from .chats import incoming_pic, pic_version

router = APIRouter(prefix="/api/chats", tags=["chat-sync"])

# Local offset older bridge builds' naive timestamps were written in (Harare).
LEGACY_NAIVE_OFFSET_MINUTES = int(os.environ.get("WA_TZ_OFFSET_MINUTES", "120"))
# Prune safety valve: never delete more than this many rows, or this fraction
# of the chat's bridge rows in the window, whichever is larger, in one sync.
PRUNE_MIN_ROWS = 40
PRUNE_MAX_FRACTION = 0.5
# An app-sent message and its WhatsApp echo are the same message if their text
# matches and WhatsApp's time is within this long of the app's.
ECHO_WINDOW = timedelta(minutes=30)
WA_SELF_SENDER = "You (WhatsApp)"


def _utc(ts: Optional[str]) -> Optional[str]:
    return to_utc_iso(ts, LEGACY_NAIVE_OFFSET_MINUTES)


def _alias_chat_ids(conn, target_id: str, name: str) -> list[str]:
    """Other chats whose name/wa_name cleans to `name` (same person, created
    under a polluted name by an older bridge)."""
    out = []
    for r in conn.execute("SELECT id, name, wa_name FROM chats WHERE id != ?", (target_id,)):
        if name in (clean_chat_name(r["name"]), clean_chat_name(r["wa_name"])):
            out.append(r["id"])
    return out


def _merge_chat_into(conn, src_id: str, dst_id: str) -> dict:
    """Move everything from chat src into dst (dst must exist), dropping
    messages dst already has (same external_key), then delete src."""
    have = {r[0] for r in conn.execute(
        "SELECT external_key FROM messages WHERE chat_id=? AND external_key IS NOT NULL", (dst_id,))}
    moved = dropped = 0
    for r in conn.execute("SELECT id, external_key FROM messages WHERE chat_id=?", (src_id,)).fetchall():
        if r["external_key"] and r["external_key"] in have:
            conn.execute("DELETE FROM messages WHERE id=?", (r["id"],))
            dropped += 1
        else:
            conn.execute("UPDATE messages SET chat_id=? WHERE id=?", (dst_id, r["id"]))
            if r["external_key"]:
                have.add(r["external_key"])
            moved += 1
    for table in ("invoices", "polls"):
        conn.execute(f"UPDATE {table} SET chat_id=? WHERE chat_id=?", (dst_id, src_id))
    # keep a profile picture if only the alias had one
    conn.execute(
        "UPDATE chats SET profile_pic_url = COALESCE(profile_pic_url, "
        "(SELECT profile_pic_url FROM chats WHERE id=?)) WHERE id=?", (src_id, dst_id))
    conn.execute("DELETE FROM chats WHERE id=?", (src_id,))
    return {"moved": moved, "duplicates_dropped": dropped}


def _upsert_chat(conn, chat_id: str, name: str, avatar: str, pic: Optional[str]) -> None:
    if conn.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone():
        conn.execute(
            "UPDATE chats SET name=?, wa_name=?, avatar_emoji=?, "
            "profile_pic_url=COALESCE(?, profile_pic_url) WHERE id=?",
            (name, name, avatar or "?", incoming_pic(pic), chat_id))
    else:
        conn.execute(
            "INSERT INTO chats (id, name, avatar_emoji, profile_pic_url, wa_name, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, name, avatar or "?", incoming_pic(pic), name, datetime.now(timezone.utc).isoformat()))


@router.post("/{chat_id}/sync")
async def sync_chat(chat_id: str, body: ChatSnapshotIn,
                    prune: bool = Query(True), force: bool = Query(False)):
    name = clean_chat_name(body.chat.name) or clean_chat_name(body.chat.wa_name)
    if not name:
        raise HTTPException(422, f"chat name {body.chat.name!r} has no real name in it")
    if chat_slug(name) != chat_id or body.chat.id != chat_id:
        raise HTTPException(422, f"chat id {chat_id} does not match name {name!r} "
                                 f"(expected {chat_slug(name)})")

    conn = get_db()
    report = {"chat_id": chat_id, "name": name, "inserted": 0, "updated": 0, "unchanged": 0,
              "linked_app_messages": 0, "dropped_invalid": 0, "pruned": 0,
              "timestamps_normalized": 0, "aliases_merged": 0, "missing_images": []}
    new_ids: list[str] = []
    try:
        conn.execute("BEGIN")
        _upsert_chat(conn, chat_id, name, body.chat.avatar_emoji, body.chat.profile_pic_url)
        for alias in _alias_chat_ids(conn, chat_id, name):
            _merge_chat_into(conn, alias, chat_id)
            report["aliases_merged"] += 1

        rows = [dict(r) for r in conn.execute(
            "SELECT id, external_key, wa_msg_id, direction, sender, kind, text, created_at, wa_status "
            "FROM messages WHERE chat_id=? ORDER BY created_at, rowid", (chat_id,))]
        by_key = {r["external_key"]: r for r in rows if r["external_key"]}
        by_wa = {r["wa_msg_id"]: r for r in rows if r["wa_msg_id"]}
        app_sent = [r for r in rows
                    if r["direction"] == "out" and r["wa_status"] == "sent" and not r["wa_msg_id"]]
        kept: set[str] = set()
        seen_keys: set[str] = set()

        for m in body.messages:
            created = to_utc_iso(m.created_at, 0)
            text = (m.text or "").strip()
            kind = "image" if m.kind == "image" else "text"
            direction = m.direction if m.direction in ("in", "out") else "in"
            if (not created or m.external_key in seen_keys
                    or (kind == "text" and (not text or is_system_notice(text) or is_time_only(text)))):
                report["dropped_invalid"] += 1
                continue
            seen_keys.add(m.external_key)
            sender = WA_SELF_SENDER if direction == "out" else ((m.sender or "").strip() or name)

            existing = by_key.get(m.external_key) or (by_wa.get(m.wa_id) if m.wa_id else None)
            if existing:
                kept.add(existing["id"])
                if existing["wa_status"] is not None:      # composed in the app: never rewritten
                    report["unchanged"] += 1
                    continue
                new = {"direction": direction, "sender": sender, "text": text,
                       "created_at": created, "wa_msg_id": m.wa_id or existing["wa_msg_id"],
                       "external_key": m.external_key}
                if any(existing[k] != v for k, v in new.items()):
                    conn.execute(
                        "UPDATE messages SET direction=?, sender=?, text=?, created_at=?, "
                        "wa_msg_id=?, external_key=? WHERE id=?",
                        (*new.values(), existing["id"]))
                    report["updated"] += 1
                else:
                    report["unchanged"] += 1
                continue

            if kind == "image":
                report["missing_images"].append(m.external_key)
                continue

            if direction == "out":
                wa_time = datetime.fromisoformat(created)
                echo = next((r for r in app_sent if r["id"] not in kept
                             and echo_key(r["text"]) == echo_key(text)
                             and _utc(r["created_at"])
                             and abs(datetime.fromisoformat(_utc(r["created_at"])) - wa_time) <= ECHO_WINDOW),
                            None)
                if echo:
                    conn.execute("UPDATE messages SET wa_msg_id=? WHERE id=?", (m.wa_id, echo["id"]))
                    kept.add(echo["id"])
                    report["linked_app_messages"] += 1
                    continue

            msg_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO messages (id, chat_id, sender, kind, text, poll_id, created_at, "
                "external_key, wa_msg_id, direction, wa_status) "
                "VALUES (?, ?, ?, 'text', ?, NULL, ?, ?, ?, ?, NULL)",
                (msg_id, chat_id, sender, text, created, m.external_key, m.wa_id, direction))
            kept.add(msg_id)
            new_ids.append(msg_id)
            report["inserted"] += 1

        ws, we = to_utc_iso(body.window_start, 0), to_utc_iso(body.window_end, 0)
        if prune and ws:
            we = we or datetime.now(timezone.utc).isoformat(timespec="microseconds")
            in_window = [r for r in rows if r["external_key"] and r["wa_status"] is None
                         and _utc(r["created_at"]) and ws <= _utc(r["created_at"]) <= we]
            stale = [r["id"] for r in in_window if r["id"] not in kept]
            limit = max(PRUNE_MIN_ROWS, int(PRUNE_MAX_FRACTION * len(in_window)))
            if len(stale) > limit and not force:
                report["prune_skipped"] = (f"would remove {len(stale)} of {len(in_window)} rows "
                                           f"(limit {limit}); check the snapshot, then sync with force=true")
            elif stale:
                for i in range(0, len(stale), 500):
                    chunk = stale[i:i + 500]
                    conn.execute(f"DELETE FROM messages WHERE id IN ({','.join('?' * len(chunk))})", chunk)
                report["pruned"] = len(stale)

        for r in conn.execute("SELECT id, created_at FROM messages WHERE chat_id=?", (chat_id,)).fetchall():
            fixed = _utc(r["created_at"])
            if fixed and fixed != r["created_at"]:
                conn.execute("UPDATE messages SET created_at=? WHERE id=?", (fixed, r["id"]))
                report["timestamps_normalized"] += 1
        conn.commit()
        # Which profile picture the server has (content hash), so the bridge
        # re-uploads after a Space restart wiped it, and only then.
        pic = conn.execute("SELECT profile_pic_url FROM chats WHERE id=?", (chat_id,)).fetchone()
        report["profile_pic_version"] = pic_version(pic["profile_pic_url"] if pic else None)
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()

    if report["inserted"] or report["updated"] or report["pruned"] or report["aliases_merged"] \
            or report["timestamps_normalized"]:
        await manager.broadcast(chat_id, {"type": "refresh", "data": {"sync": True}})
    return report


@router.post("/repair-names")
def repair_names(apply: bool = Query(False), delete_unrecoverable: bool = Query(False)):
    """Merge every chat whose name carries WhatsApp row boilerplate ("1 unread
    message\\nKarl") into the chat for its clean name ("Karl"). Dry run by
    default. Chats with no real name left at all are listed; with
    delete_unrecoverable=true those with only bridge-imported messages (nothing
    composed in the app, no invoices) are deleted."""
    conn = get_db()
    plan, unrecoverable = [], []
    for r in conn.execute("SELECT id, name, wa_name FROM chats").fetchall():
        name = clean_chat_name(r["name"]) or clean_chat_name(r["wa_name"])
        if not name:
            unrecoverable.append({"id": r["id"], "name": r["name"]})
        elif chat_slug(name) != r["id"] or name != r["name"]:
            plan.append({"from": r["id"], "raw_name": r["name"], "to": chat_slug(name), "name": name})

    deleted = []
    if apply:
        try:
            conn.execute("BEGIN")
            for p in plan:
                if p["from"] == p["to"]:
                    conn.execute("UPDATE chats SET name=?, wa_name=? WHERE id=?", (p["name"], p["name"], p["to"]))
                    continue
                _upsert_chat(conn, p["to"], p["name"], "?", None)
                p.update(_merge_chat_into(conn, p["from"], p["to"]))
            if delete_unrecoverable:
                for u in unrecoverable:
                    busy = conn.execute(
                        "SELECT (SELECT COUNT(*) FROM messages WHERE chat_id=? AND wa_status IS NOT NULL)"
                        " + (SELECT COUNT(*) FROM invoices WHERE chat_id=?)", (u["id"], u["id"])).fetchone()[0]
                    if busy:
                        continue
                    conn.execute("DELETE FROM messages WHERE chat_id=?", (u["id"],))
                    conn.execute("DELETE FROM polls WHERE chat_id=?", (u["id"],))
                    conn.execute("DELETE FROM chats WHERE id=?", (u["id"],))
                    deleted.append(u["id"])
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            raise
    conn.close()
    return {"applied": apply, "merges": plan, "unrecoverable": unrecoverable,
            "unrecoverable_deleted": deleted}
