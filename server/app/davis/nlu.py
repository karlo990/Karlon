"""davis/nlu.py — the preprocessing pass: turns a guest's WhatsApp text into
features (intents and booking slots) with plain rules, no model, in well
under a millisecond. Everything Davis decides first comes from here — whether
to answer at all (gate.py), what stage the booking is in (policy.py) — and the
model only phrases the reply.

The slot set is the classic frame of a travel-booking dialogue system (city,
dates, party size, budget, chosen option, name, ID), after GUS (Bobrow et al.
1977) and the dialog state tracking literature (Williams, Raux & Henderson
2016): each message fills or overwrites slots, and the frame, not the raw
transcript, is the conversation's memory.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Optional

CITIES = [
    "Harare", "Bulawayo", "Victoria Falls", "Mutare", "Gweru", "Masvingo",
    "Kwekwe", "Kadoma", "Chinhoyi", "Marondera", "Bindura", "Kariba",
    "Chegutu", "Beitbridge", "Gwanda", "Zvishavane", "Chiredzi", "Rusape",
    "Chipinge", "Norton",
]

# Suburbs and nicknames guests use instead of the city name. The scraper's
# own neighbourhood names are added at runtime (catalogue.places()).
PLACE_ALIASES = {
    "vic falls": "victoria falls", "vicfalls": "victoria falls", "vic-falls": "victoria falls",
    "the falls": "victoria falls", "vf": "victoria falls",
    "byo": "bulawayo", "bulawayo": "bulawayo", "hre": "harare", "harare": "harare",
    # Harare
    "borrowdale": "harare", "avondale": "harare", "greendale": "harare", "mount pleasant": "harare",
    "mt pleasant": "harare", "highlands": "harare", "belgravia": "harare", "marlborough": "harare",
    "mabelreign": "harare", "chisipite": "harare", "glen lorne": "harare", "hatfield": "harare",
    "msasa": "harare", "eastlea": "harare", "milton park": "harare", "newlands": "harare",
    "pomona": "harare", "vainona": "harare", "gunhill": "harare", "ballantyne park": "harare",
    "groombridge": "harare", "emerald hill": "harare", "strathaven": "harare", "waterfalls": "harare",
    "borrowdale brooke": "harare", "helensvale": "harare", "mandara": "harare", "greystone park": "harare",
    "cbd": None,
    # Bulawayo
    "hillside": "bulawayo", "burnside": "bulawayo", "khumalo": "bulawayo", "suburbs": "bulawayo",
    "killarney": "bulawayo", "kumalo": "bulawayo", "morningside": "bulawayo", "famona": "bulawayo",
    "bradfield": "bulawayo", "parklands": "bulawayo", "matsheumhlope": "bulawayo",
    # Mutare / Nyanga side
    "murambi": "mutare", "fairbridge park": "mutare", "darlington": "mutare",
}

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_ORD = r"(?:st|nd|rd|th)?"
_TO = r"(?:-|–|—|to|till|til|until|through|thru|and)"

NUMBER_WORDS = {
    "one": 1, "a": 1, "an": 1, "single": 1, "two": 2, "couple": 2, "pair": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "fifteen": 15, "twenty": 20,
}
_NUM = r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty)"
ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4,
            "fifth": 5, "5th": 5, "last": -1}


def _n(tok: str) -> Optional[int]:
    tok = (tok or "").lower()
    if tok.isdigit():
        return int(tok)
    return NUMBER_WORDS.get(tok)


def _has(t: str, *phrases: str) -> bool:
    return any(re.search(r"(?<![a-z])" + p + r"(?![a-z])", t) for p in phrases)


# ─────────────────────────── word limit ──────────────────────────────────────

def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def clip_words(text: str, limit: int) -> str:
    """At most `limit` words, cut at the last sentence end that fits, else at
    the word limit (WhatsApp formatting characters kept)."""
    text = re.sub(r"[ \t]+", " ", (text or "").strip())
    words = re.findall(r"\S+\s*", text)
    if len(words) <= limit:
        return text
    head = "".join(words[:limit]).rstrip()
    cut = max(head.rfind(". "), head.rfind("? "), head.rfind("! "), head.rfind("\n"))
    if cut >= len(head) // 2:
        return head[:cut + 1].rstrip()
    return head.rstrip(",;:-–") + "…"


# ─────────────────────────── dates ───────────────────────────────────────────

def _mk(day: int, month: int, year: Optional[int], today: date) -> Optional[date]:
    try:
        if year:
            return date(year if year > 100 else 2000 + year, month, day)
        d = date(today.year, month, day)
        return d if d >= today else date(today.year + 1, month, day)
    except ValueError:
        return None


def last_month(texts: list[str]) -> Optional[int]:
    """The month most recently named in these messages ("…22 November" → 11),
    so a later "21 to 22" can be read as 21–22 November."""
    month = None
    for t in texts:
        for m in re.finditer(_MON + r"(?![a-z])", (t or "").lower()):
            month = MONTHS[m.group(1)[:3]]
    return month


def parse_dates(text: str, today: date, month_hint: Optional[int] = None) -> dict:
    """check_in / check_out / nights from free text. Day-first for numeric
    dates (Zimbabwe writes 11/12 for 11 December). A check-out that would
    fall before the check-in rolls into the next year ("11 Dec till 8 Jan").
    A day range with no month ("21 to 22") uses month_hint, the month the
    conversation last named. A single date after "check out" is the check-out."""
    t = " " + text.lower().replace(",", " ") + " "
    found: list[tuple[int, date]] = []
    taken: list[tuple[int, int]] = []

    def free(a: int, b: int) -> bool:
        return all(b <= s or a >= e for s, e in taken)

    def add(pos: int, d: Optional[date], span: tuple[int, int]) -> None:
        if d and free(*span):
            found.append((pos, d))
            taken.append(span)

    # "11-14 Dec", "11 to 14 December" (one month for both days)
    for m in re.finditer(r"(?<!\d)(\d{1,2})" + _ORD + r"\s*" + _TO + r"\s*(?:the\s+)?(\d{1,2})" + _ORD
                         + r"\s+(?:of\s+)?" + _MON + r"(?:\s+(\d{4}))?", t):
        mon, yr = MONTHS[m.group(3)[:3]], m.group(4)
        a = _mk(int(m.group(1)), mon, int(yr) if yr else None, today)
        b = _mk(int(m.group(2)), mon, a.year if a else None, today) if a else None
        # "December 11th till 8th January" is two dates, not 11–8 January
        if re.search(_MON + r"\s*$", t[:m.start()]) or not (a and b and b > a):
            continue
        if free(*m.span()):
            found += [(m.start(), a), (m.start() + 1, b)]
            taken.append(m.span())
    # "Dec 11-14"
    for m in re.finditer(_MON + r"\s+(\d{1,2})" + _ORD + r"\s*" + _TO + r"\s*(\d{1,2})" + _ORD
                         + r"(?:\s+(\d{4}))?", t):
        mon = MONTHS[m.group(1)[:3]]
        a = _mk(int(m.group(2)), mon, int(m.group(4)) if m.group(4) else None, today)
        b = _mk(int(m.group(3)), mon, a.year if a else None, today) if a else None
        if re.match(r"\s*(?:of\s+)?" + _MON, t[m.end():]) or not (a and b and b > a):
            continue
        if free(*m.span()):
            found += [(m.start(), a), (m.start() + 1, b)]
            taken.append(m.span())
    # ISO 2026-12-11
    for m in re.finditer(r"(?<!\d)(20\d\d)-(\d{1,2})-(\d{1,2})(?!\d)", t):
        add(m.start(), _mk(int(m.group(3)), int(m.group(2)), int(m.group(1)), today), m.span())
    # 11/12, 11/12/2026, 11.12.2026, 11-12-26. Without a year only "/" counts:
    # "12.50" is a price, "2.5" a bath count, "11-12" a range of days.
    for m in re.finditer(r"(?<![\d$.])(\d{1,2})([/.\-])(\d{1,2})(?:\2(\d{2,4}))?(?![\d%])", t):
        day, mon, yr = int(m.group(1)), int(m.group(3)), m.group(4)
        if not yr and m.group(2) != "/":
            continue
        if re.search(r"(?:\$|usd|us\$)\s*$", t[max(0, m.start() - 5):m.start()]):
            continue
        if 1 <= mon <= 12:
            add(m.start(), _mk(day, mon, int(yr) if yr else None, today), m.span())
    # 11th December, 11 of dec
    for m in re.finditer(r"(?<!\d)(\d{1,2})" + _ORD + r"\s*(?:of\s+)?" + _MON + r"(?:\s+(\d{4}))?(?![a-z])", t):
        add(m.start(), _mk(int(m.group(1)), MONTHS[m.group(2)[:3]], int(m.group(3)) if m.group(3) else None,
                           today), m.span())
    # December 11th
    for m in re.finditer(_MON + r"\s*(\d{1,2})" + _ORD + r"(?:\s+(\d{4}))?(?!\d)", t):
        add(m.start(), _mk(int(m.group(2)), MONTHS[m.group(1)[:3]], int(m.group(3)) if m.group(3) else None,
                           today), m.span())
    # today / tonight / tomorrow / weekend
    rel = [(r"\btonight\b|\btoday\b", 0), (r"\btomorrow\b|\btmrw\b|\btmr\b", 1)]
    for rx, off in rel:
        for m in re.finditer(rx, t):
            add(m.start(), today + timedelta(days=off), m.span())
    m = re.search(r"\b(this|next|the)?\s*weekend\b", t)
    if m and free(*m.span()):
        fri = today + timedelta(days=(4 - today.weekday()) % 7)
        if m.group(1) == "next" and (fri - today).days < 7:
            fri += timedelta(days=7)
        found += [(m.start(), fri), (m.start() + 1, fri + timedelta(days=2))]
        taken.append(m.span())

    # "21 to 22" with the month named earlier in the chat
    if month_hint and not found:
        m = re.search(r"(?<![\d:/.$])(\d{1,2})" + _ORD + r"\s*(?:-|–|to|till|until)\s*(\d{1,2})" + _ORD
                      + r"(?![\d:/.%]|\s*(?:am|pm|h\b|hrs|nights?|days?|people|guests?|pax|bed|usd|dollars|\$))", t)
        if m:
            a = _mk(int(m.group(1)), month_hint, None, today)
            b = _mk(int(m.group(2)), month_hint, a.year if a else None, today) if a else None
            if a and b and b > a:
                found += [(m.start(), a), (m.start() + 1, b)]

    found.sort()
    dates = [d for _, d in found]
    out: dict = {}
    if len(dates) == 1 and re.search(r"check[- ]?out|leav(?:e|ing)|depart", t) \
            and not re.search(r"check[- ]?in|arriv", t):
        return {"check_out": dates[0].isoformat()}            # "check out … 22 November"
    nights = None
    m = re.search(r"(?<![a-z\d])" + _NUM + r"\s*(?:nights?|nyts?|days?)\b", t)
    if m:
        nights = _n(m.group(1))
    elif re.search(r"\b(?:a|one|1)\s+week\b", t):
        nights = 7
    elif re.search(r"\b(?:two|2)\s+weeks\b", t):
        nights = 14
    elif re.search(r"\b(?:a|one)\s+night\b|\bovernight\b", t):
        nights = 1
    if dates:
        ci = dates[0]
        out["check_in"] = ci.isoformat()
        co = next((d for d in dates[1:] if d != ci), None)
        if co and co < ci:
            try:
                co = co.replace(year=co.year + 1)
            except ValueError:
                co = None
        if co and (co - ci).days <= 90:
            out["check_out"] = co.isoformat()
        elif nights:
            out["check_out"] = (ci + timedelta(days=nights)).isoformat()
    if nights:
        out["nights"] = nights
    return out


# ─────────────────────────── slots ───────────────────────────────────────────

def parse_guests(t: str) -> Optional[int]:
    adults = re.search(r"(?<![a-z\d])" + _NUM + r"\s*(?:adults?|grown[- ]?ups?)\b", t)
    kids = re.search(r"(?<![a-z\d])" + _NUM + r"\s*(?:kids?|children|child|teens?|infants?|babies|baby)\b", t)
    if adults or kids:
        return (_n(adults.group(1)) if adults else 0) + (_n(kids.group(1)) if kids else 0) or None
    m = re.search(r"(?<![a-z\d])" + _NUM + r"\s*(?:guests?|people|persons?|pax|ppl|of us|peeps|visitors|"
                  r"family members|individuals|ladies|guys|men|women|friends)\b", t)
    if m:
        return _n(m.group(1))
    m = re.search(r"\b(?:family|group|party|team) of\s+" + _NUM + r"\b", t)
    if m:
        return _n(m.group(1))
    m = re.search(r"\bwe are\s+" + _NUM + r"\b", t)
    if m:
        return _n(m.group(1))
    if re.search(r"\b(?:me and my|myself and my|with my) (?:wife|husband|partner|girlfriend|boyfriend|fianc[eé]e?)\b"
                 r"|\b(?:a|for a|for the|we are a) couple\b|\bfor two\b|\bfor 2\b", t):
        return 2
    if re.search(r"\b(?:just me|only me|myself only|for myself|solo|alone)\b", t):
        return 1
    return None


def parse_budget(t: str) -> tuple[Optional[float], Optional[str]]:
    m = (re.search(r"(?:\$|usd|us\$|us dollars?)\s*(\d{2,5}(?:[.,]\d{1,2})?)", t)
         or re.search(r"(?<![\d.])(\d{2,5}(?:[.,]\d{1,2})?)\s*(?:\$|usd|us\$|dollars?|bucks)", t)
         or re.search(r"\b(?:budget|under|below|max(?:imum)?|less than|not more than|around|about)\s*(?:is\s*|of\s*)?"
                      r"(\d{2,5})\b(?!\s*(?:guests?|people|nights?|days?|bed))", t))
    if not m:
        return None, None
    value = float(m.group(1).replace(",", "."))
    basis = ("night" if re.search(r"per night|a night|/night|/n\b|\bpn\b|nightly|each night|per day|a day", t)
             else "total" if re.search(r"\btotal\b|whole stay|for everything|all in|altogether", t) else None)
    return value, basis


_NAME_STOP = {"looking", "interested", "fine", "good", "okay", "ok", "here", "from", "in", "at", "coming",
              "travelling", "traveling", "asking", "trying", "not", "so", "very", "just", "still", "well",
              "the", "a", "an", "booking", "inquiring", "enquiring", "requesting", "planning", "glad", "happy",
              "sorry", "available", "ready", "done", "back", "waiting", "on", "with", "going", "staying",
              "yes", "no", "please", "pls", "thanks", "thank", "you", "hello", "hi", "hey", "sure", "alright",
              "sir", "madam", "maam", "can", "will", "send", "me", "it", "my", "name", "is", "we", "are", "i",
              "to", "and", "for", "of", "that", "this", "option", "house", "kcer", "how", "much", "what",
              "when", "where", "morning", "afternoon", "evening", "night", "nights", "today", "tomorrow",
              "weekend", "people", "guests", "booked", "book", "paid", "noted", "great", "perfect", "cool"}
_ZW_ID = re.compile(r"(?<![\dA-Za-z])(\d{2})\s?-?\s?(\d{6,7})\s?-?\s?([A-Za-z])\s?-?\s?(\d{2})(?![\dA-Za-z])")
_PASSPORT = re.compile(r"(?<![A-Za-z\d])([A-Z]{1,2}\d{6,8})(?![A-Za-z\d])")
_PHONE = re.compile(r"^(?:\+?263|0)7\d{8}$")


def parse_id(raw: str) -> Optional[str]:
    m = _ZW_ID.search(raw)
    if m:
        return f"{m.group(1)}-{m.group(2)} {m.group(3).upper()} {m.group(4)}"
    m = re.search(r"\b(?:id|i\.d\.?|passport|national id|id number|id no)\b[\s:#.\-is]*([A-Za-z0-9\-]{6,15})", raw,
                  re.IGNORECASE)
    if m and any(c.isdigit() for c in m.group(1)):
        return m.group(1).upper()
    m = _PASSPORT.search(raw)
    if m:
        return m.group(1)
    return None


def long_number(raw: str) -> Optional[str]:
    """A bare 6–13 digit number (an ID when Davis just asked for one), not a phone number."""
    for m in re.finditer(r"(?<![\d+])(\d{6,13})(?!\d)", raw.replace(" ", "")):
        if not _PHONE.match(m.group(1)):
            return m.group(1)
    return None


def parse_name(raw: str) -> Optional[str]:
    m = re.search(r"\b(?:my (?:full )?names? (?:is|are)|names?\s*[:\-]|full names?\s*[:\-]?|"
                  r"this is|i am|i'm|im)\s+([A-Za-z][A-Za-z'\-]+(?:\s+[A-Za-z][A-Za-z'\-]+){0,3})", raw, re.IGNORECASE)
    if not m:
        return None
    words = []
    for w in m.group(1).split():
        if w.lower() in _NAME_STOP or w.lower() in MONTHS:
            break
        words.append(w)
    if not words or (len(words) == 1 and not m.group(0).lower().startswith(("my name", "name", "my full"))):
        return None
    return " ".join(w[:1].upper() + w[1:] for w in words)


def name_like(raw: str) -> Optional[str]:
    """The whole message is a person's name ("Lynford Takudzwa Tapfumaneyi"),
    optionally followed by an ID. Used only when Davis just asked for it."""
    text = _ZW_ID.sub(" ", raw)
    text = re.sub(r"\d+", " ", text)
    words = [w for w in re.split(r"[\s,]+", text.strip()) if w]
    if 2 <= len(words) <= 4 and all(re.fullmatch(r"[A-Za-z][A-Za-z'\-]+", w) for w in words) \
            and not any(w.lower() in _NAME_STOP or w.lower() in MONTHS for w in words):
        return " ".join(w[:1].upper() + w[1:] for w in words)
    return None


def parse_city(t: str, places: Optional[dict] = None) -> Optional[str]:
    table = dict(PLACE_ALIASES)
    for c in CITIES:
        table[c.lower()] = c.lower()
    for k, v in (places or {}).items():
        table.setdefault(k.lower(), v.lower() if v else v)
    best = None
    for name, city in table.items():
        if not city or len(name) < 3 and name not in ("vf",):
            continue
        m = re.search(r"(?<![a-z])" + re.escape(name) + r"(?![a-z])", t)
        if m and (best is None or len(name) > best[0]):     # longest match wins ("borrowdale brooke")
            best = (len(name), city)
    return best[1] if best else None


def parse_option(t: str) -> dict:
    out = {}
    m = re.search(r"\bkcer\s*-?\s*(\d{2,5})\b", t)
    if m:
        out["option_ref"] = int(m.group(1))
    m = re.search(r"\b(?:option|opt|number|no\.?|#)\s*(\d)\b", t)
    if m:
        out["option_no"] = int(m.group(1))
    else:
        m = (re.search(r"\b(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th|last)\s+(?:one|option|house|place)\b", t)
             or re.search(r"\b(?:take|want|choose|like|prefer|go with|book)\s+the\s+"
                          r"(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th|last)\b", t))
        if m:
            out["option_no"] = ORDINALS[m.group(1)]
    return out


# ─────────────────────────── intents ─────────────────────────────────────────

def extract(raw: str, today: Optional[date] = None, places: Optional[dict] = None,
            month_hint: Optional[int] = None) -> dict:
    """All features of one message. Pure function: same text, same result."""
    today = today or date.today()
    raw = (raw or "").strip()
    t = " ".join(raw.lower().split())
    f: dict = {"words": word_count(raw), "empty": not t}
    if not t:
        return f
    bare = re.sub(r"[^\w\s]", "", t).strip()

    f["greeting"] = _has(t, "hi+", "hello", "hey", "hie", "howzit", "good (?:morning|afternoon|evening|day)",
                         "mhoro", "makadii", "salibonani", "sawubona", "greetings")
    f["thanks"] = _has(t, "thank(?:s| you)?", "thx", "appreciated?", "ndatenda", "ngiyabonga", "siyabonga")
    f["affirm"] = bare in {"yes", "yeah", "yep", "yah", "ya", "sure", "ok", "okay", "k", "kk", "alright",
                           "go ahead", "please do", "perfect", "great", "sounds good", "fine", "cool", "yes please",
                           "ok thanks", "okay thanks", "noted", "done", "yebo", "hongu"} \
        or _has(t, "go ahead", "send (?:it|them|the invoice|me the invoice)", "i(?:'ll| will) take it",
                "let'?s do it", "book it", "proceed") or t in {"👍", "👌", "✅", "🙏"}
    f["negative"] = bare in {"no", "nope", "nah", "no thanks", "not now", "not interested", "aiwa"} \
        or _has(t, "not interested", "no thank", "too expensive", "changed my mind")
    f["ack"] = (f["affirm"] or f["thanks"]) and f["words"] <= 4 and "?" not in t
    f["opt_out"] = bare in {"stop", "unsubscribe", "stop messaging", "stop texting", "remove me", "opt out"} \
        or _has(t, "stop (?:messaging|texting|sending|contacting) me", "don'?t (?:message|text|contact) me",
                "leave me alone", "unsubscribe")
    f["opt_in"] = bare in {"start", "resume", "unstop"}
    f["human_request"] = _has(t, "(?:speak|talk|chat) (?:to|with) (?:a |an |the |your )?(?:human|person|agent|manager|"
                              "someone|somebody|real person|owner|staff|team)", "call me", "can (?:i|you) call",
                              "phone me", "give me a call", "voice note", "human please", "real person",
                              "customer (?:care|service)")
    f["bot_question"] = _has(t, "are you a (?:bot|robot|machine|ai)", "is this a (?:bot|robot)", "am i talking to a")
    f["complaint"] = _has(t, "refund", "scam(?:mer)?", "fraud", "complain(?:t)?", "terrible", "worst", "angry",
                          "disappointed", "cheat(?:ed)?", "police", "lawyer", "money back", "not happy", "rude",
                          "unacceptable", "disgusting", "useless", "rip ?off", "ridiculous", "horrible", "awful",
                          "dirty", "filthy", "smell(?:y|s|ed)?", "cockroach(?:es)?", "bed ?bugs?", "(?:was|were) broken",
                          "no (?:water|electricity|power|hot water|wifi)", "(?:was|is|were) (?:a )?(?:mess|nightmare)",
                          "not as (?:advertised|described|pictured)", "never again", "fed up")
    f["payment_claim"] = _has(t, "paid", "payment (?:done|made|sent)", "(?:sent|transferred) (?:the )?(?:money|funds|payment)",
                              "proof of payment", "pop", "ecocashed", "(?:sent|paid|transferred)(?: it| the money)? (?:via|on|by|through) ecocash", "swiped", "deposit(?:ed| made)",
                              "here is the proof", "i have paid", "made the payment", "bank transfer done")
    f["terms_request"] = _has(t, "terms", "t ?& ?c'?s?", "t'?s and c'?s", "conditions", "cancellation", "policy",
                              "policies", "house rules", "rules")
    f["invoice_request"] = _has(t, "invoice", "quotation", "quote", "bill me", "how (?:do|can) i pay", "banking details",
                                "bank details", "payment details", "how to pay", "where do i pay", "pay now",
                                "ecocash number", "ecocash details", "which number", "what number")
    f["price_query"] = _has(t, "how much", "price", "prices", "pricing", "cost", "costs", "rate", "rates", "charges?",
                            "per night", "fee", "fees", "affordable", "cheap(?:est)?")
    f["availability_query"] = _has(t, "available", "availability", "vacan(?:t|cy)", "accommodation", "place to stay",
                                   "looking for", "book(?:ing)?", "reserve", "reservation", "house", "houses", "lodge",
                                   "lodges", "place", "somewhere", "apartment", "flat", "cottage", "villa", "airbnb", "rooms?", "stay",
                                   "self[- ]catering", "holiday home", "guest ?house")
    f["more_options"] = _has(t, "more options", "other options", "anything else", "something (?:else|cheaper|bigger)",
                             "cheaper", "bigger", "smaller", "more houses", "others", "different (?:one|house|place)",
                             "show me more", "any more")
    f["explicit_choice"] = _has(t, "take", "want", "book", "choose", "go with", "like", "prefer", "reserve",
                                "i'?ll have", "we'?ll have", "that one", "this one")
    f["viewing_request"] = _has(t, "see the (?:place|house|home|property|flat|apartment|room)s?(?: first)?",
                                "view(?:ing)? the (?:place|house|home|property)", "viewings?", "come (?:and|to) see",
                                "visit the (?:place|house|home|property)", "inspect(?:ion)?", "physical(?:ly)? (?:see|check)",
                                "check (?:it|the place|the house) out(?: first)?", "can i come (?:over|through|and see)")
    f["checkin_query_raw"] = _has(t, "check[- ]?in time", "check[- ]?out time", "what time (?:is|do|can) (?:we |i )?"
                              "(?:check|arrive|come|leave)", "when (?:can|do) (?:we|i) (?:check in|arrive|come in|leave)",
                              "early check[- ]?in", "late check[- ]?out")
    f["location_query"] = _has(t, "where (?:is|are) (?:it|they|the house|this)", "location", "address", "directions",
                               "pin", "how far")
    f["long_term"] = _has(t, "per month", "monthly", "/month", "p/m", "pm rent", "long[- ]term",
                          "lease", "not booking", "room to rent", "rooms to rent", "to rent a room",
                          "rent (?:a|the) room", "for rent")
    f["photos_request"] = _has(t, "pics?", "pictures?", "photos?", "images?", "more pics")
    f["question"] = "?" in t or bool(re.match(r"(what|where|when|how|can|could|is|are|do|does|will|would|which|who)\b", t))
    # Asking about check-in times, not stating their own ("check out time 10:00am 22November").
    f["checkin_query"] = f.pop("checkin_query_raw") and (f["question"] or bool(re.search(r"\bwhat time\b", t)))

    f.update(parse_dates(t, today, month_hint))
    guests = parse_guests(t)
    if guests and 1 <= guests <= 40:
        f["guests"] = guests
    m = re.search(r"(?<![a-z\d])" + _NUM + r"\s*(?:bed(?:room)?s?|br|bdr)\b", t)
    if m and _n(m.group(1)):
        f["bedrooms"] = _n(m.group(1))
    budget, basis = parse_budget(t)
    if budget:
        f["budget_usd"], f["budget_basis"] = budget, basis
    city = parse_city(t, places)
    if city:
        f["city"] = city
    f.update(parse_option(t))
    if re.fullmatch(r"\d", bare):
        f["bare_number"] = int(bare)
    ident = parse_id(raw)
    if ident:
        f["id_number"] = ident
    num = long_number(raw)
    if num:
        f["long_number"] = num
    name = parse_name(raw)
    if name:
        f["full_name"] = name
    nl = name_like(raw)
    if nl:
        f["name_like"] = nl
    return f


SLOT_KEYS = ("city", "check_in", "check_out", "nights", "guests", "bedrooms", "budget_usd", "budget_basis",
             "full_name", "id_number")


def merge_slots(slots: dict, feats: dict) -> dict:
    """Newest mention wins per slot; a new check-in without a check-out drops
    the old check-out (it belonged to other dates)."""
    out = dict(slots)
    if feats.get("check_in") and not feats.get("check_out") and feats["check_in"] != out.get("check_in"):
        out.pop("check_out", None)
    for k in SLOT_KEYS:
        if feats.get(k) not in (None, ""):
            out[k] = feats[k]
    if out.get("check_in") and not out.get("check_out") and out.get("nights"):
        try:
            out["check_out"] = (date.fromisoformat(out["check_in"]) + timedelta(days=int(out["nights"]))).isoformat()
        except ValueError:
            pass
    if out.get("check_in") and out.get("check_out"):
        try:
            out["nights"] = (date.fromisoformat(out["check_out"]) - date.fromisoformat(out["check_in"])).days
        except ValueError:
            pass
    return out
