"""routers/chats.py — chat list + chat create/update."""

import hashlib
import os
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, File, HTTPException, UploadFile

from ..config import DISPLAY_TZ_OFFSET_MINUTES, PROFILE_PIC_MAX_BYTES, PROFILE_PICS_DIR
from ..database import get_db
from ..ws_manager import manager
from ..models import UpsertChatIn
from ..wa_clean import clean_chat_name, to_offset_iso

router = APIRouter(prefix="/api/chats", tags=["chats"])

# Same shape as wa_bridge.py's _PHONE_RE — a chat is an "unsaved number" when
# its WhatsApp name is just a raw phone number rather than a saved contact
# name. Surfaced to clients as `is_unsaved` so the UI can mark it (e.g. a
# small colored dot) instead of relying on an emoji glued onto the name.
_PHONE_RE = re.compile(r"^\+\d[\d\s\-().]{6,}$")


def _is_unsaved(wa_name: str | None, name: str) -> bool:
    candidate = (wa_name or name or "").strip()
    return bool(_PHONE_RE.match(candidate))


PIC_PREFIX = "/static/profile_pics/"
_PIC_EXT = {b"\xff\xd8\xff": "jpg", b"\x89PNG": "png"}


def _pic_ext(data: bytes) -> Optional[str]:
    for magic, ext in _PIC_EXT.items():
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def live_pic_url(url: Optional[str]) -> Optional[str]:
    """The stored picture URL, or None when it points at a profile picture
    file this server doesn't have: the Space's disk is wiped on every
    restart, and older bridges stored a path that only existed on the PC.
    The app then shows the initials instead of a broken image."""
    if not url or not url.startswith(PIC_PREFIX):
        return url or None
    name = os.path.basename(url[len(PIC_PREFIX):].split("?", 1)[0])
    return url if name and (PROFILE_PICS_DIR / name).is_file() else None


def pic_version(url: Optional[str]) -> Optional[str]:
    """The ?v= content hash of a live uploaded picture (wa_bridge compares it
    with what WhatsApp shows to decide whether to upload)."""
    url = live_pic_url(url)
    if url and url.startswith(PIC_PREFIX) and "?v=" in url:
        return url.split("?v=", 1)[1]
    return None


def incoming_pic(url: Optional[str]) -> Optional[str]:
    """A profile_pic_url sent with a chat upsert. Paths under
    /static/profile_pics/ are only ever set by the upload endpoint below, so
    a client-sent one (an old bridge's PC-local path) is ignored."""
    url = (url or "").strip()
    return None if not url or url.startswith(PIC_PREFIX) else url


@router.get("")
def list_chats():
    conn = get_db()
    rows = conn.execute(
        """
        SELECT c.id, c.name, c.avatar_emoji, c.profile_pic_url, c.wa_name,
               (SELECT CASE WHEN kind='image' THEN COALESCE(NULLIF(text,''), '\U0001f4f7 Photo')
                            ELSE text END
                FROM messages m WHERE m.chat_id = c.id
                ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1) AS last_text,
               (SELECT created_at FROM messages m WHERE m.chat_id = c.id
                ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1) AS last_at,
               (SELECT direction FROM messages m WHERE m.chat_id = c.id
                ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1) AS last_direction
        FROM chats c
        ORDER BY (last_at IS NULL), last_at DESC
        """
    ).fetchall()
    conn.close()
    result = [dict(r) for r in rows]
    for r in result:
        r["is_unsaved"] = _is_unsaved(r.get("wa_name"), r.get("name", ""))
        r["profile_pic_url"] = live_pic_url(r.get("profile_pic_url"))
        if r.get("last_at"):
            r["last_at"] = to_offset_iso(r["last_at"], DISPLAY_TZ_OFFSET_MINUTES)
    return result


@router.post("")
def upsert_chat(body: UpsertChatIn):
    """
    Create or update a chat.  Called every sync cycle from wa_bridge —
    wa_name carries the raw WhatsApp display name (before any prefix we add)
    so the outbox sender can open the correct WA chat row via DOM.
    """
    # Older bridges sent names polluted with WhatsApp's hidden unread label
    # ("1 unread message\nKarl"); store the real name. (The id is the
    # caller's; /api/chats/repair-names merges chats created under such names.)
    body.name = clean_chat_name(body.name) or body.name
    body.wa_name = clean_chat_name(body.wa_name) or body.wa_name
    conn = get_db()
    existing = conn.execute("SELECT 1 FROM chats WHERE id=?", (body.id,)).fetchone()
    wa_name = (body.wa_name or "").strip() or None

    if existing:
        conn.execute(
            # A plain upsert never clears a picture uploaded via /profile-pic.
            "UPDATE chats SET name=?, avatar_emoji=?, profile_pic_url=COALESCE(?, profile_pic_url), "
            "wa_name=? WHERE id=?",
            (body.name.strip(), body.avatar_emoji.strip() or "?",
             incoming_pic(body.profile_pic_url), wa_name, body.id),
        )
    else:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO chats (id, name, avatar_emoji, profile_pic_url, wa_name, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (body.id, body.name.strip(), body.avatar_emoji.strip() or "?",
             incoming_pic(body.profile_pic_url), wa_name, now),
        )
    conn.commit()
    row = dict(conn.execute("SELECT * FROM chats WHERE id=?", (body.id,)).fetchone())
    conn.close()
    row["profile_pic_url"] = live_pic_url(row.get("profile_pic_url"))
    return row


@router.post("/{chat_id}/profile-pic")
async def upload_profile_pic(chat_id: str, file: UploadFile = File(...)):
    """The chat's WhatsApp profile picture, uploaded by wa_bridge.py. Stored
    as /static/profile_pics/<chat id>.<ext>; the URL carries ?v=<content
    hash> so the app's image cache picks up a changed picture."""
    data = await file.read(PROFILE_PIC_MAX_BYTES + 1)
    if len(data) > PROFILE_PIC_MAX_BYTES:
        raise HTTPException(413, "profile picture is larger than 2 MB")
    ext = _pic_ext(data)
    if not ext:
        raise HTTPException(415, "profile picture must be a JPEG, PNG or WebP image")
    conn = get_db()
    try:
        if not conn.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone():
            raise HTTPException(404, "chat not found")
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", chat_id)
        for old in PROFILE_PICS_DIR.glob(f"{safe}.*"):
            if old.suffix != f".{ext}":
                old.unlink(missing_ok=True)
        tmp = PROFILE_PICS_DIR / f".{safe}.{ext}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, PROFILE_PICS_DIR / f"{safe}.{ext}")
        version = hashlib.sha1(data).hexdigest()[:12]
        url = f"{PIC_PREFIX}{safe}.{ext}?v={version}"
        conn.execute("UPDATE chats SET profile_pic_url=? WHERE id=?", (url, chat_id))
        conn.commit()
    finally:
        conn.close()
    await manager.broadcast(chat_id, {"type": "refresh", "data": {"profile_pic": True}})
    return {"chat_id": chat_id, "profile_pic_url": url, "version": version}