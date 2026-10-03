"""routers/broadcast.py — one message from the front desk into every chat."""

import hashlib
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter

from ..database import get_db
from ..models import BroadcastIn, serialize_message
from ..ws_manager import manager

router = APIRouter(tags=["broadcast"])


@router.post("/api/broadcast")
async def broadcast_to_all(body: BroadcastIn):
    """
    Send the same text to every chat.  Marked direction='out' + wa_status=
    'pending' so wa_bridge delivers the message to every WhatsApp contact
    automatically.

    Guards against the same broadcast text being sent to the same chat
    more than once — e.g. a double-click on "send", a retried request, or
    a scheduler firing more often than intended. Without this, calling
    this endpoint repeatedly with the same promo text (as happened here —
    "Karl_Con Consultancy... Looking for your next getaway?..." went out
    4 times to the same contact) queues a brand-new pending message per
    call with no awareness that an identical one is already pending or
    was already delivered recently. A chat is skipped if the exact same
    text (whitespace-normalized) is either still pending, or was sent
    successfully within the cooldown window below.
    """
    conn = get_db()
    text = body.text.strip()
    text_key = hashlib.sha1(" ".join(text.split()).lower().encode()).hexdigest()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()

    chat_ids = [r["id"] for r in conn.execute("SELECT id FROM chats").fetchall()]

    sent = []
    skipped = []
    for chat_id in chat_ids:
        # Skip if this exact text is already pending, or was sent to this
        # chat within the last 24h — prevents the same broadcast piling up
        # or re-firing as a near-duplicate a few minutes later.
        dup = conn.execute(
            """
            SELECT 1 FROM messages
            WHERE chat_id = ?
              AND direction = 'out'
              AND (
                    wa_status = 'pending'
                    OR (wa_status = 'sent' AND created_at >= datetime(?, '-1 day'))
                  )
              AND lower(replace(replace(trim(text), char(10), ' '), char(13), ' ')) = ?
            LIMIT 1
            """,
            (chat_id, now, " ".join(text.lower().split())),
        ).fetchone()
        if dup:
            skipped.append(chat_id)
            continue

        msg_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO messages "
            "(id, chat_id, sender, kind, text, poll_id, created_at, direction, wa_status, external_key) "
            "VALUES (?, ?, ?, 'broadcast', ?, NULL, ?, 'out', 'pending', ?)",
            (msg_id, chat_id, body.sender.strip() or "Front Desk", text, now, text_key),
        )
        sent.append((chat_id, msg_id))
    conn.commit()

    for chat_id, msg_id in sent:
        msg = serialize_message(
            conn, conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
        )
        await manager.broadcast(chat_id, {"type": "message", "data": msg})
    conn.close()
    return {"sent_to": len(sent), "skipped_duplicate": len(skipped)}