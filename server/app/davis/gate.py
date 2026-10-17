"""davis/gate.py — should Davis answer this chat right now? Pure function of
the recent messages, the session and the settings; every "no" has a reason
that shows on the dashboard.

Turn-taking (Sacks, Schegloff & Jefferson 1974): a reply belongs after the
guest's turn ends. Davis waits for the guest to stop typing (debounce, in
engine.py), answers only when the newest message is the guest's, sends at
most MAX_REPLIES_PER_TURN messages, then waits for the guest again. A staff
member speaking in the chat takes the floor: Davis steps back.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..wa_clean import echo_key
from . import config


def is_davis(sender: Optional[str]) -> bool:
    return (sender or "").strip().lower().startswith("davis")


def parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def speaking(m: dict) -> bool:
    """Messages that take a turn: anything the guest sends, and text from our
    side. Photos and PDFs coming back from WhatsApp (copies of what Davis or
    the app sent) don't end the guest's turn.

    Messages are in ARRIVAL order (rowid), not by timestamp: WhatsApp gives
    the guest's messages minute-precision times, so a quick reply would sort
    before the Davis message it answers."""
    return m.get("direction") == "in" or m.get("kind", "text") == "text"


def latest_inbound_index(messages: list[dict]) -> Optional[int]:
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("direction") == "in":
            return i
    return None


def turn(messages: list[dict]) -> list[dict]:
    """The guest's current turn: their messages since anyone last replied."""
    out = []
    for m in reversed(messages):
        if not speaking(m):
            continue
        if m.get("direction") != "in":
            break
        out.append(m)
    return list(reversed(out))


def davis_replies_this_turn(messages: list[dict]) -> int:
    """Davis's own text replies since the guest's newest message (listing
    cards, photos and documents ride along with a reply; they don't count)."""
    i = latest_inbound_index(messages)
    after = messages[i + 1:] if i is not None else messages
    return sum(1 for m in after if m.get("direction") == "out" and m.get("sender") == config.SENDER
               and m.get("kind", "text") == "text")


def _looks_like_document_bubble(text: str) -> bool:
    t = (text or "").lower()
    return ".pdf" in t or (" page" in t and ("kb" in t or "mb" in t)) or t.startswith("karlcon_")


def staff_spoke_recently(messages: list[dict], now: datetime, minutes: int) -> Optional[dict]:
    """A message a person on the team typed (in the app, or on the phone),
    within the window. Not staff: Davis's own messages and their WhatsApp
    echoes, photos and documents coming back from WhatsApp."""
    if minutes <= 0:
        return None
    since = now - timedelta(minutes=minutes)
    davis_keys = {echo_key(m.get("text") or "") for m in messages
                  if m.get("direction") == "out" and is_davis(m.get("sender")) and m.get("text")}
    for m in reversed(messages):
        ts = parse_ts(m.get("created_at"))
        if ts and ts < since:
            break
        if m.get("direction") != "out" or is_davis(m.get("sender")) or m.get("kind", "text") != "text":
            continue
        text = m.get("text") or ""
        if not text.strip() or _looks_like_document_bubble(text) or echo_key(text) in davis_keys:
            continue
        return m
    return None


def sender_key(sender: Optional[str]) -> str:
    """One key per person however WhatsApp writes the name: "+263 78 655 7599"
    and "+263786557599" are the same sender (a phone number keeps its last 9
    digits, so 0786557599 matches too); names compare letters and digits only."""
    s = (sender or "").strip().lower()
    digits = re.sub(r"\D", "", s)
    if digits and len(digits) >= 7 and not re.search(r"[a-z]", s):
        return digits[-9:]
    return re.sub(r"[^a-z0-9]", "", s)


def is_group(messages: list[dict]) -> bool:
    senders = {sender_key(m.get("sender")) for m in messages if m.get("direction") == "in"}
    senders -= {"", "unknown"}
    return len(senders) > 1


def decide(ctx: dict, turn_feats: dict, resume: bool = False) -> dict:
    """{"respond": bool, "reason": str}. resume=True is a continuation Davis
    promised the guest (live prices arrived), so the guest needn't have
    written last."""
    s, st, msgs = ctx["session"], ctx["settings"], ctx["messages"]
    now = ctx["now"]

    def no(reason: str) -> dict:
        return {"respond": False, "reason": reason}

    if st.get("mode") == "off":
        return no("mode_off")
    if st.get("paused"):
        return no("paused_globally")
    if st.get("only_chats") and ctx["chat_id"] not in st["only_chats"]:
        return no("not_in_only_chats")
    if ctx["chat_id"] in (st.get("ignore_chats") or []):
        return no("ignored_chat")
    if not msgs:
        return no("no_messages")
    if is_group(msgs):
        return no("group_chat")
    i = latest_inbound_index(msgs)
    if i is None:
        return no("guest_never_wrote")
    latest = msgs[i]
    if not resume:
        last = next((m for m in reversed(msgs) if speaking(m)), None)
        if not last or last.get("direction") != "in":
            return no("last_message_not_from_guest")
        if s.get("last_inbound_id") == latest["id"]:
            return no("already_handled")
    ts = parse_ts(latest.get("created_at"))
    if not ts or now - ts > timedelta(minutes=config.FRESH_MINUTES + (60 if resume else 0)):
        return no("guest_message_too_old")
    if s.get("opted_out") and not turn_feats.get("opt_in"):
        return no("opted_out")
    if s.get("handoff"):                 # only "Take over" on the dashboard mutes Davis
        return no("staff_took_over")
    if s.get("paused"):
        return no("chat_paused")
    staff = staff_spoke_recently(msgs, now, config.HUMAN_QUIET_MINUTES)
    if staff:
        return no("staff_replied_recently")
    if davis_replies_this_turn(msgs) >= config.MAX_REPLIES_PER_TURN:
        return no("reply_limit_reached")
    if not resume and turn_feats.get("ack_only"):
        last_davis = next((m for m in reversed(msgs[:i]) if m.get("direction") == "out"
                           and m.get("sender") == config.SENDER and m.get("text")), None)
        if not (last_davis and last_davis["text"].rstrip().endswith("?")):
            return no("acknowledgement_only")
    return {"respond": True, "reason": "resume" if resume else "guest_waiting"}
