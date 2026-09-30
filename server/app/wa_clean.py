"""wa_clean.py — what counts as good vs bad WhatsApp data. Pure functions, no I/O.

The SAME file ships in two places and must stay byte-identical (a test
enforces it):
    pc-bridge/wa_clean.py    used by wa_bridge.py when it scans a chat
    server/app/wa_clean.py   used by the server when it accepts a snapshot
so the bridge and the server can never disagree about what a valid chat name,
a real message or a timestamp is. The bridge filters first; the server
re-applies the same rules so that an older bridge build (or any other client)
can't reintroduce bad rows.

Rules, and why each exists (all observed in real Karlon data):

* Chat names. WhatsApp Web renders a visually-hidden "1 unread message" label
  next to the contact name for screen readers. Reading the row's text picked
  it up, so chats were created as "1 unread message\\nKarl". Because a chat's
  id is a hash of its name, the id changed every time the unread count did:
  one person became several chats, none of them called "Karl".
  clean_chat_name() drops that label (and other row boilerplate) and returns
  the real name, or "" if nothing real is left.

* System notices ("Messages and calls are end-to-end encrypted…", "This
  message was deleted", unread/date dividers) are WhatsApp UI, not messages.

* Timestamps. WhatsApp shows the bubble time as a separate element; on some
  layouts it was captured as the last line of the caption ("…fix it or…\\n
  20:16"). split_trailing_time() recovers it as the time instead of text.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

# ── chat names ─────────────────────────────────────────────────────────────

_UNREAD_RE = re.compile(r"^\d+\s+unread\s+messages?$", re.IGNORECASE)
_UNREAD_HEAD_RE = re.compile(r"^\s*(\d+)\s+unread\s+", re.IGNORECASE)
_UNREAD_SUFFIX_RE = re.compile(
    r"\s*[,:;·•|\-–—]*\s*\d+\s+unread\s+messages?\s*$", re.IGNORECASE)
_SEPARATORS = " \t,:;·•|-–—"


def _strip_unread_prefix(s: str) -> str:
    """Remove a leading "N unread message(s)" label. The count decides
    singular vs plural, so a label glued straight onto the name
    ("1 unread messageSteve") doesn't eat the name's first letter."""
    m = _UNREAD_HEAD_RE.match(s)
    if not m:
        return s
    word = "message" if int(m.group(1)) == 1 else "messages"
    rest = s[m.end():]
    if not rest.lower().startswith(word):
        return s
    rest = rest[len(word):]
    if word == "message" and rest[:1] == "s" and (len(rest) == 1 or rest[1] in _SEPARATORS):
        rest = rest[1:]                  # "1 unread messages" (ungrammatical, but seen)
    return rest.lstrip(_SEPARATORS)
TIME_RE = re.compile(r"^\d{1,2}[:.]\d{2}(\s?[AaPp]\.?\s?[Mm]\.?)?$")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_NAME_BOILERPLATE = {
    "archived", "pinned", "muted", "pinned chat", "muted chat", "draft",
    "online", "typing…", "typing...", "recording audio…", "recording audio...",
    "today", "yesterday", "you", "business account", "unread",
    "click here for contact info", "click here for group info", "click here for more info",
    *_WEEKDAYS,
}


def _is_name_boilerplate(s: str) -> bool:
    low = s.strip().lower()
    return (
        not low
        or low in _NAME_BOILERPLATE
        or bool(_UNREAD_RE.match(low))
        or bool(TIME_RE.match(low))
        or low.startswith("last seen")
        or bool(re.match(r"^\d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4}$", low))
    )


# Invisible characters WhatsApp wraps around names and phone numbers
# (bidi embedding marks, zero-width joiners/spaces, BOM).
_INVISIBLE_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")
_PHONE_NAME_RE = re.compile(r"^\+\d[\d\s\-().]{6,}$")


def clean_chat_name(raw: Optional[str]) -> str:
    """The real chat name inside whatever the sidebar row / header yielded,
    or "" if it contained nothing but boilerplate.

    Phone numbers come back in one canonical form ("+263780438459"): the same
    unsaved contact was seen both as "+263 78 043 8459" and "+263780438459",
    and since a chat's id is a hash of its name, that was two chats."""
    if not raw:
        return ""
    raw = _INVISIBLE_RE.sub("", raw)
    for line in re.split(r"[\r\n]+", raw):
        s = line
        for _ in range(3):  # "2 unread messages · 1 unread message" etc.
            s = _strip_unread_prefix(s)
            s = _UNREAD_SUFFIX_RE.sub("", s)
        s = re.sub(r"\s+", " ", s).strip()
        if s and not _is_name_boilerplate(s):
            if _PHONE_NAME_RE.match(s):
                return "+" + re.sub(r"\D", "", s)
            return s
    return ""


def chat_slug(name: str) -> str:
    """Stable chat id. Must match what every bridge build has used."""
    return "wa-" + hashlib.md5(name.strip().encode()).hexdigest()[:12]


# ── messages ───────────────────────────────────────────────────────────────

_SYSTEM_NOTICE_RES = [re.compile(p, re.IGNORECASE) for p in (
    r"^messages (and calls )?(to this (group|chat) )?are (now )?(end-to-end encrypted|secured)",
    r"^(this|your) (business|chat) (uses|is now using|is with) (a )?(secure service|business account)",
    r"^this business (uses|is now using|works with) ",
    r"^chats? with businesses",
    r"^waiting for this message",
    r"^(this message was deleted|you deleted this message)\.?$",
    r"security code (with .{1,80} )?changed",
    r"^missed (voice|video|group voice|group video) call",
    r"^(you )?turned (on|off) disappearing messages",
    r"^disappearing messages (were|are) turned (on|off)",
    r"uses a default timer for disappearing messages",
    r"^you (blocked|unblocked) this (contact|business)",
    r"changed (their|his|her) phone number to a new number",
    r"joined using this group's invite link",
    r"^tap to learn more\.?$|^click to learn more\.?$",
)]


def is_unread_divider(text: str) -> bool:
    return bool(_UNREAD_RE.match((text or "").strip()))


def is_system_notice(text: str) -> bool:
    t = (text or "").strip()
    return bool(t) and (is_unread_divider(t) or any(r.search(t) for r in _SYSTEM_NOTICE_RES))


def is_time_only(text: str) -> bool:
    return bool(TIME_RE.match((text or "").strip()))


def split_trailing_time(text: str) -> tuple[str, Optional[str]]:
    """("body", "20:16") if the last line of `text` is a bare bubble time
    (optionally preceded by an "Edited" line), else (text, None). A one-line
    text is never split — a message that *is* just a time is judged by
    is_time_only(), not silently emptied here."""
    lines = (text or "").rstrip().split("\n")
    if len(lines) < 2 or not TIME_RE.match(lines[-1].strip()):
        return (text or "").strip(), None
    t = lines[-1].strip()
    body = lines[:-1]
    if body and body[-1].strip().lower() == "edited":
        body = body[:-1]
    return "\n".join(body).strip(), t


def normalize_text(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip().lower())


# ── dates & times ──────────────────────────────────────────────────────────

_PRE_RE = re.compile(r"^\[(.*?)\]\s*(.*?):\s*$")
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def parse_pre_plain(pre: str) -> tuple[str, str]:
    """'[20:16, 29/09/2026] Karl: ' -> ('20:16, 29/09/2026', 'Karl')."""
    m = _PRE_RE.match(pre or "")
    return (m.group(1).strip(), m.group(2).strip()) if m else ("", "")


def _parse_time(t: str) -> Optional[tuple[int, int]]:
    m = re.match(r"^(\d{1,2})[:.](\d{2})\s*([AaPp])?\.?\s*[Mm]?\.?$", (t or "").strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    ap = (m.group(3) or "").lower()
    if ap == "p" and h < 12:
        h += 12
    elif ap == "a" and h == 12:
        h = 0
    return (h, mi) if h < 24 and mi < 60 else None


def _parse_numeric_date(s: str, dayfirst: bool) -> Optional[date]:
    m = re.match(r"^(\d{1,4})[/.\-](\d{1,2})[/.\-](\d{1,4})$", (s or "").strip())
    if not m:
        return None
    a, b, c = (int(x) for x in m.groups())
    try:
        if a > 31:                       # yyyy-mm-dd
            return date(a, b, c)
        y = c + 2000 if c < 100 else c
        d, mo = (a, b) if dayfirst else (b, a)
        return date(y, mo, d)
    except ValueError:
        return None


def detect_dayfirst(pre_values: list[str], default: bool = True) -> bool:
    """dd/mm vs mm/dd from the dates this chat actually printed. Any first
    field > 12 means day-first; any second field > 12 means month-first."""
    for pre in pre_values:
        ts, _ = parse_pre_plain(pre)
        m = re.search(r"(\d{1,2})[/.\-](\d{1,2})[/.\-]\d{2,4}", ts)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > 12 >= b:
                return True
            if b > 12 >= a:
                return False
    return default


def parse_pre_timestamp(ts_raw: str, dayfirst: bool = True) -> Optional[datetime]:
    """'20:16, 29/09/2026' or '8:16 PM, 9/29/2026' -> naive local datetime."""
    parts = [p.strip() for p in (ts_raw or "").split(",")]
    if len(parts) < 2:
        return None
    hm = _parse_time(parts[0])
    d = _parse_numeric_date(parts[1], dayfirst)
    if not hm or not d:
        return None
    return datetime(d.year, d.month, d.day, hm[0], hm[1])


def parse_divider_date(text: str, today: date, dayfirst: bool = True) -> Optional[date]:
    """Date for a WhatsApp date divider ('TODAY', 'Yesterday', 'Monday',
    '29/09/2026', '29 September 2026', 'September 29, 2026'), else None."""
    t = re.sub(r"\s+", " ", (text or "").strip().lower()).strip(" ,.")
    if not t:
        return None
    if t == "today":
        return today
    if t == "yesterday":
        return today - timedelta(days=1)
    if t in _WEEKDAYS:
        back = (today.weekday() - _WEEKDAYS.index(t)) % 7 or 7
        return today - timedelta(days=back)
    d = _parse_numeric_date(t, dayfirst)
    if d:
        return d
    m = (re.match(r"^(\d{1,2}) ([a-z]+),? (\d{4})$", t)
         or re.match(r"^([a-z]+) (\d{1,2}),? (\d{4})$", t)
         or re.match(r"^(\d{1,2}) ([a-z]+)$", t)
         or re.match(r"^([a-z]+) (\d{1,2})$", t))
    if not m:
        return None
    g = m.groups()
    day_s, mon_s = (g[0], g[1]) if g[0].isdigit() else (g[1], g[0])
    mon = _MONTHS.get(mon_s[:3])
    if not mon:
        return None
    year = int(g[2]) if len(g) > 2 else today.year
    try:
        d = date(year, mon, int(day_s))
    except ValueError:
        return None
    if len(g) == 2 and d > today:        # "Dec 30" seen in January = last year
        d = date(year - 1, mon, int(day_s))
    return d


def combine(d: date, t: str) -> Optional[datetime]:
    hm = _parse_time(t)
    return datetime(d.year, d.month, d.day, hm[0], hm[1]) if hm else None


def to_utc_iso(ts: Optional[str], naive_offset_minutes: int = 0) -> Optional[str]:
    """Any ISO timestamp -> canonical UTC 'YYYY-MM-DDTHH:MM:SS.ffffff+00:00'
    (None if unparseable). A naive value is taken to be local time at
    `naive_offset_minutes` east of UTC — how older bridge builds sent
    WhatsApp's times. One fixed format matters because created_at is sorted
    as a string: naive local times mixed with UTC used to interleave wrongly
    by the zone offset (2 h in Harare)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(minutes=naive_offset_minutes)))
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def to_offset_iso(ts: Optional[str], offset_minutes: int) -> Optional[str]:
    """A stored timestamp re-expressed at a fixed UTC offset, e.g. Harare
    (+02:00): '2026-09-30T10:13:00+00:00' -> '2026-09-30T12:13:00.000000+02:00'.
    Same instant, local wall-clock digits. Storage stays UTC (it's what sorts);
    this is for display, because a client that shows the ISO digits as-is (the
    Android app did) otherwise showed every time two hours early. Unparseable
    input is returned unchanged."""
    utc = to_utc_iso(ts, offset_minutes)
    if utc is None:
        return ts
    tz = timezone(timedelta(minutes=offset_minutes))
    return datetime.fromisoformat(utc).astimezone(tz).isoformat(timespec="microseconds")
