"""routers/chats.py — chat list + chat create/update."""

import re
from datetime import datetime, timezone

from fastapi import APIRouter

from ..database import get_db
from ..models import UpsertChatIn

router = APIRouter(prefix="/api/chats", tags=["chats"])

# Same shape as wa_bridge.py's _PHONE_RE — a chat is an "unsaved number" when
# its WhatsApp name is just a raw phone number rather than a saved contact
# name. Surfaced to clients as `is_unsaved` so the UI can mark it (e.g. a
# small colored dot) instead of relying on an emoji glued onto the name.
_PHONE_RE = re.compile(r"^\+\d[\d\s\-().]{6,}$")


def _is_unsaved(wa_name: str | None, name: str) -> bool:
    candidate = (wa_name or name or "").strip()
    return bool(_PHONE_RE.match(candidate))


@router.get("")
def list_chats():
    conn = get_db()
    rows = conn.execute(
        """
        SELECT c.id, c.name, c.avatar_emoji, c.profile_pic_url, c.wa_name,
               (SELECT CASE WHEN kind='image' THEN COALESCE(NULLIF(text,''), '\U0001f4f7 Photo')
                            ELSE text END
                FROM messages m WHERE m.chat_id = c.id
                ORDER BY m.created_at DESC LIMIT 1) AS last_text,
               (SELECT created_at FROM messages m WHERE m.chat_id = c.id
                ORDER BY m.created_at DESC LIMIT 1) AS last_at,
               (SELECT direction FROM messages m WHERE m.chat_id = c.id
                ORDER BY m.created_at DESC LIMIT 1) AS last_direction
        FROM chats c
        ORDER BY (last_at IS NULL), last_at DESC
        """
    ).fetchall()
    conn.close()
    result = [dict(r) for r in rows]
    for r in result:
        r["is_unsaved"] = _is_unsaved(r.get("wa_name"), r.get("name", ""))
    return result


@router.post("")
def upsert_chat(body: UpsertChatIn):
    """
    Create or update a chat.  Called every sync cycle from wa_bridge —
    wa_name carries the raw WhatsApp display name (before any prefix we add)
    so the outbox sender can open the correct WA chat row via DOM.
    """
    conn = get_db()
    existing = conn.execute("SELECT 1 FROM chats WHERE id=?", (body.id,)).fetchone()
    wa_name = (body.wa_name or "").strip() or None

    if existing:
        conn.execute(
            "UPDATE chats SET name=?, avatar_emoji=?, profile_pic_url=?, wa_name=? WHERE id=?",
            (body.name.strip(), body.avatar_emoji.strip() or "?",
             body.profile_pic_url, wa_name, body.id),
        )
    else:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO chats (id, name, avatar_emoji, profile_pic_url, wa_name, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (body.id, body.name.strip(), body.avatar_emoji.strip() or "?",
             body.profile_pic_url, wa_name, now),
        )
    conn.commit()
    row = conn.execute("SELECT * FROM chats WHERE id=?", (body.id,)).fetchone()
    conn.close()
    return dict(row)