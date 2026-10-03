"""routers/messages.py — text/image data path + outbox for WA delivery."""

import hashlib
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi import Query

from ..config import MEDIA_DIR, MEDIA_KIND_BY_EXT, TERMS_PDF_PATH
from ..database import get_db
from ..models import ImportMessagesIn, SendMessageIn, serialize_message
from ..wa_clean import is_system_notice, is_time_only, to_utc_iso
from ..ws_manager import manager
from .chat_sync import LEGACY_NAIVE_OFFSET_MINUTES

router = APIRouter(prefix="/api/chats", tags=["messages"])


# ─────────────────────────── read ────────────────────────────────────────────

@router.get("/{chat_id}/messages")
def get_messages(chat_id: str):
    conn = get_db()
    if not conn.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone():
        conn.close()
        raise HTTPException(404, "chat not found")
    rows = conn.execute(
        "SELECT * FROM messages WHERE chat_id=? ORDER BY created_at ASC, rowid ASC", (chat_id,)
    ).fetchall()
    out = [serialize_message(conn, r) for r in rows]
    conn.close()
    return out


# ─────────────────────────── outbox (per-chat) ───────────────────────────────

@router.get("/{chat_id}/outbox")
def get_outbox(chat_id: str):
    """Messages sent from the app/web that are still pending WA delivery."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM messages WHERE chat_id=? AND direction='out' AND wa_status IN ('pending','documents') "
        "ORDER BY created_at ASC, rowid ASC",
        (chat_id,),
    ).fetchall()
    out = [serialize_message(conn, r) for r in rows]
    conn.close()
    return out


@router.patch("/{chat_id}/messages/{msg_id}/wa-ack")
def ack_wa_message(
    chat_id: str,
    msg_id: str,
    status: str = Query(default="sent", pattern="^(sent|error|pending|urgent|documents|superseded)$"),
):
    """
    Called by wa_bridge after attempting DOM delivery.
      status=sent    → delivered to WhatsApp ✓
      status=error   → gave up (shown as ⚠ in the app)
      status=pending → reset for retry (normal queue)
      status=urgent  → reset for retry, back into the priority queue
                       (routers/outbox.py::get_urgent_outbox) instead of
                       the normal 'pending' one
      status=superseded → a duplicate of a document already being sent
    """
    conn = get_db()
    conn.execute(
        "UPDATE messages SET wa_status=? WHERE id=? AND chat_id=?",
        (status, msg_id, chat_id),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "status": status}


# ─────────────────────────── send text ───────────────────────────────────────

@router.post("/{chat_id}/messages")
async def send_message(chat_id: str, body: SendMessageIn):
    """
    Plain-text message originating from the Karlon app or web UI.
    Marked direction='out' + wa_status='pending' so wa_bridge's outbox
    poller picks it up and delivers it to WhatsApp via DOM automation.
    """
    conn = get_db()
    if not conn.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone():
        conn.close()
        raise HTTPException(404, "chat not found")

    msg_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO messages "
        "(id, chat_id, sender, kind, text, poll_id, created_at, direction, wa_status) "
        "VALUES (?, ?, ?, 'text', ?, NULL, ?, 'out', 'pending')",
        (msg_id, chat_id, body.sender.strip() or "Guest", body.text.strip(), now),
    )
    conn.commit()
    msg = serialize_message(
        conn, conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
    )
    conn.close()

    await manager.broadcast(chat_id, {"type": "message", "data": msg})
    return msg


# ─────────────────────────── send image ──────────────────────────────────────

@router.post("/{chat_id}/images")
async def send_image(
    chat_id: str,
    sender: str = Form("Guest"),
    caption: str = Form(""),
    file: UploadFile = File(...),
):
    """Image message — from app/web, also queued for WA delivery."""
    conn = get_db()
    if not conn.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone():
        conn.close()
        raise HTTPException(404, "chat not found")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in MEDIA_KIND_BY_EXT["image"]:
        conn.close()
        raise HTTPException(400, f"unsupported image type: {ext or 'unknown'}")

    data = await file.read()
    if not data:
        conn.close()
        raise HTTPException(400, "empty file")

    fname = f"{uuid.uuid4().hex}{ext}"
    (MEDIA_DIR / fname).write_bytes(data)
    media_url = f"/static/media/{fname}"

    msg_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO messages "
        "(id, chat_id, sender, kind, text, poll_id, created_at, media_url, media_type, "
        "direction, wa_status) "
        "VALUES (?, ?, ?, 'image', ?, NULL, ?, ?, 'image', 'out', 'pending')",
        (msg_id, chat_id, sender.strip() or "Guest", caption.strip(), now, media_url),
    )
    conn.commit()
    msg = serialize_message(
        conn, conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
    )
    conn.close()

    await manager.broadcast(chat_id, {"type": "message", "data": msg})
    return msg


# ─────────────────────────── import image (wa_bridge, INBOUND) ───────────────

@router.post("/{chat_id}/images/import")
async def import_image(
    chat_id: str,
    sender: str = Form("Unknown"),
    caption: str = Form(""),
    external_key: str = Form(...),
    direction: str = Form(None),
    created_at: str = Form(None),
    file: UploadFile = File(...),
):
    """
    Scraped image from wa_bridge — either side of the conversation, not just
    inbound (see wa_bridge.import_image_message's docstring for why this
    endpoint exists at all: it replaces an older path that hardcoded every
    scraped image, in or out, as a pending outbound send and caused an
    echo-back-to-WhatsApp loop).

    Direction resolution mirrors _resolve_import_direction() in the text
    path just above: trust an explicit `direction` from the payload, else
    fall back to the "You (WhatsApp)" sender convention, else default 'in'.
    Either way wa_status stays NULL here — even a genuinely outbound image
    got to WhatsApp by being sent directly from the phone (that's how
    wa_bridge could scrape it), so there is nothing left to deliver.

    Dedup on external_key, mirroring /import.
    """
    conn = get_db()
    if not conn.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone():
        conn.close()
        raise HTTPException(404, "chat not found")

    existing = conn.execute(
        "SELECT * FROM messages WHERE external_key=? AND chat_id=?",
        (external_key, chat_id),
    ).fetchone()
    if existing:
        msg = serialize_message(conn, existing)
        conn.close()
        return msg

    ext = Path(file.filename or "").suffix.lower()
    if ext not in MEDIA_KIND_BY_EXT["image"]:
        conn.close()
        raise HTTPException(400, f"unsupported image type: {ext or 'unknown'}")

    data = await file.read()
    if not data:
        conn.close()
        raise HTTPException(400, "empty file")

    fname = f"{uuid.uuid4().hex}{ext}"
    (MEDIA_DIR / fname).write_bytes(data)
    media_url = f"/static/media/{fname}"

    resolved_direction = (
        direction if direction in ("in", "out")
        else ("out" if sender.strip() == _WA_SELF_SENDER else "in")
    )

    msg_id = str(uuid.uuid4())
    # WhatsApp's own time for the photo (so it sits in the right place in the
    # thread), else now — always stored as UTC so it sorts against app messages.
    now = (to_utc_iso(created_at, LEGACY_NAIVE_OFFSET_MINUTES)
           or datetime.now(timezone.utc).isoformat(timespec="microseconds"))
    conn.execute(
        "INSERT INTO messages "
        "(id, chat_id, sender, kind, text, poll_id, created_at, external_key, "
        "media_url, media_type, direction, wa_status) "
        "VALUES (?, ?, ?, 'image', ?, NULL, ?, ?, ?, 'image', ?, NULL)",
        (msg_id, chat_id, sender.strip() or "Unknown", caption.strip(), now,
         external_key, media_url, resolved_direction),
    )
    conn.commit()
    msg = serialize_message(
        conn, conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
    )
    conn.close()

    await manager.broadcast(chat_id, {"type": "message", "data": msg})
    return msg


# ─────────────────────────── send static T&Cs doc ─────────────────────────────

@router.post("/{chat_id}/send-terms")
async def send_terms(chat_id: str):
    """
    Queue the KARLCON Elite Retreats Terms & Conditions PDF to a guest.

    Unlike invoices (generated per-booking by invoice_worker.py), this is
    one fixed file reused for every send — so there's no worker hand-off,
    just a direct insert of a 'document' message pointing at the static
    PDF. It rides the exact same outbox -> wa_bridge -> dom_send_file path
    invoices use, so delivery (Playwright attaching it in WhatsApp Web) is
    identical for both.
    """
    conn = get_db()
    if not conn.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone():
        conn.close()
        raise HTTPException(404, "chat not found")

    if not TERMS_PDF_PATH.exists():
        conn.close()
        raise HTTPException(
            500,
            "Terms & Conditions PDF not found on server "
            f"(expected at {TERMS_PDF_PATH}); upload it before sending.",
        )

    media_url = f"/static/documents/{TERMS_PDF_PATH.name}"
    caption = "KARLCON Elite Retreats — Terms & Conditions"
    # Tapping the button again while the PDF is still waiting to be sent
    # returns the queued one instead of sending the guest two copies.
    queued = conn.execute(
        "SELECT * FROM messages WHERE chat_id=? AND direction='out' AND media_url=? "
        "AND wa_status IN ('documents','pending') ORDER BY created_at LIMIT 1",
        (chat_id, media_url),
    ).fetchone()
    if queued:
        msg = serialize_message(conn, queued)
        conn.close()
        return msg

    msg_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO messages "
        "(id, chat_id, sender, kind, text, poll_id, created_at, media_url, media_type, "
        "direction, wa_status) "
        "VALUES (?, ?, 'Front Desk', 'document', ?, NULL, ?, ?, 'document', 'out', 'documents')",
        (msg_id, chat_id, caption, now, media_url),
    )
    conn.commit()
    msg = serialize_message(
        conn, conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
    )
    conn.close()

    await manager.broadcast(chat_id, {"type": "message", "data": msg})
    return msg


# ─────────────────────────── bulk import (wa_bridge) ─────────────────────────

# A message imported from wa_bridge is "yours" if it was sent from your own
# WhatsApp number — whether that was typed in the Karlon app, the web
# dashboard, or straight into WhatsApp on your phone. wa_bridge.py's
# scrape_messages() always sets this exact sender string for such messages
# (see detect_direction() -> "You (WhatsApp)"), so it doubles as a direction
# signal for any payload that doesn't (yet) send an explicit `direction`.
_WA_SELF_SENDER = "You (WhatsApp)"


def _resolve_import_direction(item: "ImportMessageItem") -> str:
    """
    Historically this endpoint hardcoded direction='in' for every imported
    message, full stop — meaning every message you sent directly from
    WhatsApp (as opposed to from the Karlon app) was stored, and therefore
    displayed, as if it came from the customer. wa_bridge.detect_direction()
    already worked this out correctly at scrape time; this endpoint just
    never had anywhere to put that answer, because ImportMessageItem didn't
    carry a `direction` field at all. Now: trust an explicit direction if
    the payload sent one, otherwise fall back to the sender-string check
    that's been true of every wa_bridge build so far.
    """
    if item.direction in ("in", "out"):
        return item.direction
    if item.sender.strip() == _WA_SELF_SENDER:
        return "out"
    return "in"


@router.post("/{chat_id}/import")
async def import_messages(chat_id: str, body: ImportMessagesIn):
    """
    Bulk idempotent insert from wa_bridge. Direction is resolved per-message
    by _resolve_import_direction() — see there for why this isn't just a
    flat 'in' anymore.
    """
    conn = get_db()
    if not conn.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone():
        conn.close()
        raise HTTPException(404, "chat not found")

    imported, skipped, corrected = 0, 0, 0
    new_ids: list[str] = []
    corrected_ids: list[str] = []
    for m in body.messages:
        text = (m.text or "").strip()
        has_media = bool(m.media_url)
        if not text and not has_media:
            continue
        # WhatsApp UI notices ("Messages and calls are end-to-end encrypted…",
        # "1 unread message", bare "20:16") are not messages.
        if not has_media and (is_system_notice(text) or is_time_only(text)):
            skipped += 1
            continue
        ext_key = m.external_key or hashlib.sha1(
            f"{m.sender}|{text}|{m.created_at}|{m.media_url or ''}".encode("utf-8")
        ).hexdigest()
        # UTC, one format: naive local times from older bridges sorted two
        # hours off against app-sent messages.
        created_at = (to_utc_iso(m.created_at, LEGACY_NAIVE_OFFSET_MINUTES)
                      or datetime.now(timezone.utc).isoformat(timespec="microseconds"))
        msg_id = str(uuid.uuid4())
        kind = "image" if m.media_type == "image" else "text"
        direction = _resolve_import_direction(m)

        # DEDUP. There is no UNIQUE index on external_key, so the old
        # "INSERT and count the exception as skipped" never skipped anything:
        # every bridge pass re-inserted every visible message (the stacks of
        # repeated bubbles). Check explicitly instead.
        existing = conn.execute(
            "SELECT id, direction, wa_status FROM messages WHERE chat_id=? AND external_key=?",
            (chat_id, ext_key),
        ).fetchone()
        if existing:
            skipped += 1
            # Self-heal: a re-scrape that now carries an EXPLICIT direction
            # fixes rows older bridge builds stored on the wrong side. Only
            # bridge-imported rows (nothing pending delivery) are touched.
            if (m.direction in ("in", "out") and existing[1] != m.direction
                    and existing[2] is None):
                conn.execute(
                    "UPDATE messages SET direction=?, sender=? WHERE chat_id=? AND external_key=?",
                    (m.direction, m.sender.strip() or "Unknown", chat_id, ext_key),
                )
                conn.commit()
                corrected += 1
                corrected_ids.append(existing[0])
            continue

        try:
            conn.execute(
                "INSERT INTO messages "
                "(id, chat_id, sender, kind, text, poll_id, created_at, "
                "external_key, media_url, media_type, direction, wa_status) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL)",
                (msg_id, chat_id, m.sender.strip() or "Unknown", kind, text,
                 created_at, ext_key, m.media_url, m.media_type, direction),
            )
            conn.commit()
            imported += 1
            new_ids.append(msg_id)
        except Exception:
            conn.rollback()
            skipped += 1

    new_msgs = [
        serialize_message(
            conn, conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        )
        for mid in new_ids
    ]
    conn.close()

    for msg in new_msgs:
        await manager.broadcast(chat_id, {"type": "message", "data": msg})
    if corrected_ids:
        # Tell open app/web screens to re-fetch so bubbles jump to the right side.
        await manager.broadcast(chat_id, {"type": "refresh", "data": {"corrected": corrected_ids}})

    return {"imported": imported, "skipped": skipped, "corrected": corrected}


# ─────────────────────────── one-off backfill (historical rows) ──────────────

@router.post("/backfill-direction")
def backfill_direction():
    """
    One-time fix for rows imported before this fix existed.

    Text: every wa_bridge-imported message was stored as direction='in'
    even when its sender was already the "You (WhatsApp)" marker, so
    historical outbound messages still show on the wrong side until this
    runs once.

    Images: a separate, worse bug (see wa_bridge.import_image_message's
    docstring) meant every scraped image — incoming or outgoing — was
    posted to the app/staff-send endpoint, which hardcodes direction='out'
    and wa_status='pending'. That's fixed going forward (wa_bridge now
    calls /images/import instead), but for historical rows this backfill
    only touches the unambiguous slice: images where sender is literally
    "You (WhatsApp)" (only wa_bridge ever writes that exact string) get
    wa_status cleared, since they already reached WhatsApp and have nothing
    left to deliver — there's no point re-sending them.

    Deliberately NOT touched: image rows with any other sender. Those are
    genuinely ambiguous from the DB alone — a real customer's incoming
    photo mis-tagged 'out' by the old bug looks identical, sender-wise, to
    a legitimate invoice/T&Cs/listing photo actually sent to that same
    customer through the app. Guessing wrong there risks corrupting real
    business records, so leaving them as-is and flagging this instead.
    Run the read-only check below first if you want to see how many that
    is before deciding whether to handle them by hand.

    Safe to call more than once. Doesn't touch anything with sender other
    than "You (WhatsApp)".

    Call once after deploying this fix:
        curl -X POST https://<your-space>.hf.space/api/chats/backfill-direction
    """
    conn = get_db()
    text_updated = conn.execute(
        "UPDATE messages SET direction='out' "
        "WHERE sender=? AND direction IS NOT 'out'",
        (_WA_SELF_SENDER,),
    ).rowcount
    image_cleared = conn.execute(
        "UPDATE messages SET wa_status=NULL "
        "WHERE kind='image' AND sender=? AND wa_status IS NOT NULL",
        (_WA_SELF_SENDER,),
    ).rowcount
    ambiguous_images = conn.execute(
        "SELECT COUNT(*) FROM messages "
        "WHERE kind='image' AND direction='out' AND sender != ?",
        (_WA_SELF_SENDER,),
    ).fetchone()[0]
    conn.commit()
    conn.close()
    return {
        "text_direction_fixed": text_updated,
        "self_sent_images_wa_status_cleared": image_cleared,
        "ambiguous_image_rows_not_touched": ambiguous_images,
    }


# ─────────────────────── one-off cleanup of bad wa_bridge imports ─────────────

_TIME_ONLY_RE = re.compile(r"^\s*\d{1,2}[:.]\d{2}(\s?[AaPp]\.?[Mm]\.?)?\s*$")
_JUNK_PREFIXES = (
    "waiting for this message", "this message was deleted", "you deleted this message",
    "messages and calls are end-to-end encrypted",
    "your business uses a secure service from meta",
    "this business uses a secure service from meta",
    "this business is now using a secure service",
)


def _is_junk_text(text: str) -> bool:
    t = (text or "").strip()
    return (not t) or bool(_TIME_ONLY_RE.match(t)) or t.lower().startswith(_JUNK_PREFIXES)


@router.post("/cleanup-wa-import")
def cleanup_wa_import(apply: bool = Query(False), self_names: str = Query("Karl"),
                      tz_offset_hours: float = Query(2.0)):
    """
    Repairs rows older wa_bridge builds imported wrongly. Touches ONLY
    bridge-imported rows (external_key set, wa_status NULL) — never messages
    composed in the app/web.

      0. Deletes duplicate copies of the same WhatsApp message.
      1. Deletes bodiless text rows: timestamp-only ("19:55"),
         "Waiting for this message", deleted-message and WhatsApp system notices.
      3. Re-times WhatsApp-scraped rows stored as local time with no timezone
         (older bridge builds) into UTC, so they sort correctly against
         app-sent messages. tz_offset_hours = the PC's zone (Harare = 2).
      2. Moves your own messages to the right: rows whose sender is one of
         `self_names` (your WhatsApp push-name, e.g. "Karl") -> direction 'out'.

    Dry run by default (shows counts). To actually change data:
        curl -X POST "https://<space>.hf.space/api/chats/cleanup-wa-import?apply=true"
    """
    names = [n.strip().lower() for n in self_names.split(",") if n.strip()]
    conn = get_db()
    # 0. Duplicates from the old no-dedup import: keep the first copy of each
    #    (chat_id, external_key), drop the rest.
    dup_ids = [r[0] for r in conn.execute(
        "SELECT id FROM messages WHERE external_key IS NOT NULL AND rowid NOT IN ("
        "  SELECT MIN(rowid) FROM messages WHERE external_key IS NOT NULL "
        "  GROUP BY chat_id, external_key)"
    ).fetchall()]
    rows = conn.execute(
        "SELECT id, text FROM messages WHERE external_key IS NOT NULL "
        "AND wa_status IS NULL AND kind='text'"
    ).fetchall()
    junk_ids = [r[0] for r in rows if _is_junk_text(r[1])]

    self_rows = []
    if names:
        ph = ",".join("?" * len(names))
        self_rows = [r[0] for r in conn.execute(
            f"SELECT id FROM messages WHERE external_key IS NOT NULL AND wa_status IS NULL "
            f"AND LOWER(TRIM(sender)) IN ({ph}) AND (direction IS NULL OR direction != 'out')",
            names,
        ).fetchall()]

    junk_ids = [i for i in junk_ids if i not in set(dup_ids)]

    # 1b. Line fragments: older bridges also re-read multi-line messages line
    #     by line and stored each line as its own message, stamped with the
    #     import time (so they piled up at the bottom, usually on the wrong
    #     side). A fragment = single-line imported row, whose text is exactly
    #     one whole line of a multi-line message in the same chat, and whose
    #     timestamp is NOT a WhatsApp minute (WhatsApp times end in :00).
    skip = set(dup_ids) | set(junk_ids)
    frag_ids = []
    by_chat: dict = {}
    for mid, cid, text, ts in conn.execute(
        "SELECT id, chat_id, text, created_at FROM messages WHERE kind='text' AND text IS NOT NULL"
    ).fetchall():
        by_chat.setdefault(cid, []).append((mid, text, ts))
    for cid, rows_ in by_chat.items():
        lines = set()
        for mid, text, ts in rows_:
            if mid not in skip and "\n" in text:
                lines.update(l.strip() for l in text.split("\n") if l.strip())
        if not lines:
            continue
        imported = {r[0] for r in conn.execute(
            "SELECT id FROM messages WHERE chat_id=? AND external_key IS NOT NULL AND wa_status IS NULL",
            (cid,)).fetchall()}
        for mid, text, ts in rows_:
            t = (text or "").strip()
            if (mid in imported and mid not in skip and "\n" not in t and t in lines
                    and not re.search(r"T\d{2}:\d{2}:00(\.0+)?($|[+-]|Z)", ts or "")):
                frag_ids.append(mid)
    junk_ids += frag_ids

    from datetime import timedelta
    naive = []
    for mid, ts in conn.execute("SELECT id, created_at FROM messages").fetchall():
        try:
            dt = datetime.fromisoformat(ts)
        except Exception:
            continue
        if dt.tzinfo is None:
            fixed = (dt - timedelta(hours=tz_offset_hours)).replace(tzinfo=timezone.utc)
            naive.append((fixed.isoformat(timespec="microseconds"), mid))
    if apply:
        for i in range(0, len(dup_ids), 500):
            chunk = dup_ids[i:i + 500]
            conn.execute(f"DELETE FROM messages WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        for i in range(0, len(junk_ids), 500):
            chunk = junk_ids[i:i + 500]
            conn.execute(f"DELETE FROM messages WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        for i in range(0, len(self_rows), 500):
            chunk = self_rows[i:i + 500]
            conn.execute(
                f"UPDATE messages SET direction='out', sender=? "
                f"WHERE id IN ({','.join('?' * len(chunk))})",
                [_WA_SELF_SENDER, *chunk],
            )
        conn.executemany("UPDATE messages SET created_at=? WHERE id=?", naive)
        conn.commit()
    conn.close()
    return {
        "applied": apply,
        "timestamps_fixed" if apply else "timestamps_would_fix": len(naive),
        "duplicate_rows_deleted" if apply else "duplicate_rows_would_delete": len(dup_ids),
        "junk_rows_deleted" if apply else "junk_rows_would_delete": len(junk_ids),
        "of_which_line_fragments": len(frag_ids),
        "own_rows_moved_right" if apply else "own_rows_would_move_right": len(self_rows),
    }
