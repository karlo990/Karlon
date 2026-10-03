"""routers/customer_replies.py — dedicated urgent-reply line for app→customer WA delivery.

Separate from the general messages router (/api/chats/{id}/messages) so:
  1. wa_bridge can prioritise these above bulk/broadcast traffic by polling
     GET /api/outbox/urgent (routers/outbox.py) at a tighter interval
     (e.g. 2 s vs 5 s) instead of waiting in line behind the normal
     'pending' queue.
  2. The log/DB makes it obvious which messages were urgent app-originated
     replies vs general chat traffic (wa_status='urgent' vs 'pending').

This module owns only the write side: POST /api/reply/{chat_id}. The read
side (GET /api/outbox/urgent) lives solely in routers/outbox.py — an
earlier merge left a second, divergent copy of that GET route here as well,
which is why this file wouldn't even import; that duplicate is gone now,
so there is exactly one implementation of the urgent queue.

Delivery ack path is unchanged: wa_bridge still calls
  PATCH /api/chats/{chat_id}/messages/{msg_id}/wa-ack?status=sent
after DOM delivery. See ack_wa_message in messages.py.
"""

import hashlib
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from ..database import get_db
from ..models import SendMessageIn, serialize_message
from ..ws_manager import manager

router = APIRouter(prefix="/api", tags=["customer-replies"])


def _text_key(text: str) -> str:
    return hashlib.sha1(" ".join(text.split()).lower().encode()).hexdigest()


@router.post("/reply/{chat_id}")
async def send_urgent_reply(chat_id: str, body: SendMessageIn):
    """
    App-originated urgent reply to a guest/customer.

    Stored as direction='out', wa_status='urgent' so GET /api/outbox/urgent
    (routers/outbox.py) can hand it to wa_bridge ahead of the normal
    pending queue.
    """
    conn = get_db()
    if not conn.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone():
        conn.close()
        raise HTTPException(404, "chat not found")

    text = body.text.strip()
    if not text:
        conn.close()
        raise HTTPException(422, "text must not be empty")

    # Skip if the identical urgent text is already queued (unsent) for
    # this chat — guards against a double-tap on "send" queuing the same
    # reply twice, the same failure mode broadcast.py had before its fix.
    dup = conn.execute(
        """
        SELECT 1 FROM messages
        WHERE chat_id = ? AND direction = 'out' AND wa_status = 'urgent'
          AND lower(replace(replace(trim(text), char(10), ' '), char(13), ' ')) = ?
        LIMIT 1
        """,
        (chat_id, " ".join(text.lower().split())),
    ).fetchone()
    if dup:
        conn.close()
        raise HTTPException(409, "identical urgent reply is already queued for this chat")

    msg_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO messages "
        "(id, chat_id, sender, kind, text, poll_id, created_at, direction, wa_status, external_key) "
        "VALUES (?, ?, ?, 'text', ?, NULL, ?, 'out', 'urgent', ?)",
        (msg_id, chat_id, body.sender.strip() or "Front Desk", text, now, _text_key(text)),
    )
    conn.commit()
    msg = serialize_message(
        conn, conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
    )
    conn.close()

    await manager.broadcast(chat_id, {"type": "message", "data": msg})
    return msg
