"""routers/polls.py — live polls (kept from the original build)."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from ..database import get_db
from ..models import CreatePollIn, VoteIn, poll_options_with_counts, serialize_message
from ..ws_manager import manager

router = APIRouter(tags=["polls"])


@router.post("/api/chats/{chat_id}/polls")
async def create_poll(chat_id: str, body: CreatePollIn):
    options = [o.strip() for o in body.options if o.strip()]
    if len(options) < 2:
        raise HTTPException(400, "a poll needs at least 2 options")

    conn = get_db()
    if not conn.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone():
        conn.close()
        raise HTTPException(404, "chat not found")

    poll_id = str(uuid.uuid4())
    msg_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        "INSERT INTO polls (id, chat_id, question, created_at) VALUES (?, ?, ?, ?)",
        (poll_id, chat_id, body.question.strip(), now),
    )
    for opt in options:
        conn.execute(
            "INSERT INTO poll_options (id, poll_id, text) VALUES (?, ?, ?)",
            (str(uuid.uuid4()), poll_id, opt),
        )
    conn.execute(
        "INSERT INTO messages (id, chat_id, sender, kind, text, poll_id, created_at) "
        "VALUES (?, ?, ?, 'poll', ?, ?, ?)",
        (msg_id, chat_id, body.sender.strip() or "Guest", body.question.strip(), poll_id, now),
    )
    conn.commit()
    msg = serialize_message(conn, conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone())
    conn.close()

    await manager.broadcast(chat_id, {"type": "message", "data": msg})
    return msg


@router.post("/api/polls/{poll_id}/vote")
async def vote_poll(poll_id: str, body: VoteIn):
    conn = get_db()
    poll = conn.execute("SELECT * FROM polls WHERE id=?", (poll_id,)).fetchone()
    if not poll:
        conn.close()
        raise HTTPException(404, "poll not found")
    if not conn.execute(
        "SELECT 1 FROM poll_options WHERE id=? AND poll_id=?", (body.option_id, poll_id)
    ).fetchone():
        conn.close()
        raise HTTPException(404, "option not found")

    conn.execute(
        "INSERT INTO poll_votes (poll_id, option_id, voter_id) VALUES (?, ?, ?) "
        "ON CONFLICT(poll_id, voter_id) DO UPDATE SET option_id=excluded.option_id",
        (poll_id, body.option_id, body.voter_id),
    )
    conn.commit()
    options = poll_options_with_counts(conn, poll_id)
    chat_id = poll["chat_id"]
    conn.close()

    payload = {"type": "poll_update", "data": {"poll_id": poll_id, "options": options}}
    await manager.broadcast(chat_id, payload)
    return payload["data"]
