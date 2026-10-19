"""davis/mind.py — Karl's mind: how a small open model, running on the
Space's own CPU, understands a WhatsApp conversation and is asked to answer.
No API, no key: llama.cpp with a pretrained 4-bit instruct model
(local_llm.py). The design and its sources are in
.claude/skills/karl-local-llm/SKILL.md.

Why. The October 2026 chats failed on understanding, not wording: dates
written "4november", "22.3.27", "From 23 to 25" were asked for again, a name
on its own line was dropped, our own house cards' dates were taken as the
guest's, questions about photos, pools and bedrooms were ignored, a monthly
room request got holiday homes. Rules alone can't keep up with how people
write, and a 1.5B model given only the last line can't either.

The pipeline for one guest turn (neuro-symbolic: the model reads and writes,
code computes and acts):

1. Rules first (nlu.py, < 1 ms). A turn the rules fully account for
   ("KCER 105", "ok", "Harare 21-22 Nov for 4") never waits for the model
   (a cascade, after FrugalGPT, Chen, Zaharia & Zou 2023).
2. STATE (model, JSON constrained by a grammar so it is always valid and
   short: Willard & Louf 2023; llama.cpp GBNF). The model reads the recent
   conversation and writes the booking state and the guest's unanswered
   questions: dialogue state tracking by an LLM (Wu et al., Findings of
   EMNLP 2023). It also copies the guest's own date words (dates_text);
   code does the calendar (nlu.parse_dates): small models are good at
   finding which words are the dates and bad at date arithmetic.
3. Code checks the state (to_features), then policy.plan decides the actions
   (houses, live prices, invoice, Terms) and the points the reply must carry.
4. REPLY (model, grammar-constrained JSON) from the same conversation, the
   checked facts and the plan; the guest's newest messages and questions go
   last (Liu et al., "Lost in the Middle", TACL 2024).
5. policy.check_reply verifies every reply (no invented price, house or
   number, no repeat); a rejected reply is rewritten once with the reason
   (Self-Refine, Madaan et al. 2023), then the plan's own wording is used.

Speed on 2 CPU cores. STATE and REPLY share one system prompt and the same
opening of the user message (date, known state, houses, conversation); only
the TASK block at the end differs. llama.cpp keeps the evaluated prefix of
the previous prompt and a RAM cache of earlier ones, so the reply re-reads
only its own task block (prefix sharing, as in SGLang's RadixAttention,
Zheng et al. 2023).

Communication model (Shannon & Weaver 1949 with Schramm's 1954 feedback
loop): source = the guest's lines; noise = typos, splits, deleted messages,
WhatsApp notices, house cards (removed or labelled by transcript()); receiver
= STATE; destination = policy.plan; feedback = Karl restates what it
understood and asks one clear question when something is ambiguous
(grounding, Clark & Brennan 1991); channel = a phone screen, short, never a
repeated line.

Memory (the conversation cache): the recent transcript itself, read every
turn; the booking state in davis_sessions; the houses already sent
(house_sends). Nothing is summarised away.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Optional

from . import config, nlu, persona


def _null(t: dict) -> dict:
    return {"anyOf": [t, {"type": "null"}]}


_STR, _INT, _NUM, _BOOL = {"type": "string"}, {"type": "integer"}, {"type": "number"}, {"type": "boolean"}

INTENTS = ["greeting", "booking", "price", "photos", "amenities", "location", "invoice", "payment_proof",
           "terms", "complaint", "human", "opt_out", "thanks", "other"]

# Only what the rules can't read. Every generated token costs ~0.1-0.2 s on
# 2 CPU cores, so bedrooms, budget and the yes/no intents stay with nlu.py.
# Field order matters for a small model: it writes them in this order, so the
# evidence (the guest's own words) comes before the decisions.
STATE_SCHEMA = {
    "type": "object",
    "properties": {
        "city": _null(_STR),
        "dates_text": _null(_STR),
        "check_in": _null(_STR),
        "check_out": _null(_STR),
        "guests": _null(_INT),
        "stay_type": {"type": "string", "enum": ["short_stay", "long_term_rental", "unknown"]},
        "chosen_ref": _null(_INT),
        "photos_for": {"type": "array", "items": _INT},
        "full_name": _null(_STR),
        "id_number": _null(_STR),
        "open_questions": {"type": "array", "items": _STR},
    },
    "required": ["city", "dates_text", "check_in", "check_out", "guests", "stay_type", "chosen_ref", "photos_for",
                 "full_name", "id_number", "open_questions"],
    "additionalProperties": False,
}
UNDERSTAND_SCHEMA = STATE_SCHEMA            # the name the engine and tests use

REPLY_SCHEMA = {"type": "object", "properties": {"reply": _STR}, "required": ["reply"],
                "additionalProperties": False}


def _system() -> str:
    p = persona.PERSONA
    bullets = lambda xs: "\n".join(f"- {x}" for x in xs)     # noqa: E731
    return f"""You are the mind of {p['name']}, the virtual booking assistant of {persona.BRAND}. {p['company']} Guests write on WhatsApp. Each request gives you the conversation and ends with a TASK: STATE or REPLY.

THE TRANSCRIPT. GUEST is the customer. KARL is you. TEAM is our staff. [house card KCER n: ...] is a house we sent; its 📅 dates are when that house is free, NOT the guest's dates. [photos] and [PDF] are attachments.

TASK: STATE. Write the guest's booking as JSON:
- Read the whole conversation. Guests split one thought over several lines, answer late and correct themselves; the newest statement wins.
- Take details from the guest's own words. Something KARL or TEAM proposed counts only if the guest agreed.
- dates_text: copy the guest's own words for their stay dates (the corrected ones if they corrected), e.g. "in Monday 22.3.27 out Tuesday 23 3.37" or "From 23 to 25". check_in / check_out: the same as YYYY-MM-DD, day before month, the next such date on or after TODAY; null if unsure.
- guests: people staying (2 couples = 4). city: the town, lowercase (Avondale, Borrowdale, Mount Pleasant are harare; Hillside is bulawayo).
- stay_type: long_term_rental for a monthly rent, a room to rent, a lease or "not booking"; short_stay for nights; else unknown.
- chosen_ref: the KCER number the guest picked to book, else null. photos_for: KCER numbers they want pictures of ("pics" about the house just discussed = that house).
- full_name / id_number: only what the guest gave for the invoice. Never invent.
- open_questions: each question or request of the guest that no later KARL or TEAM message answered, in a few plain words, newest last.
- Keep WHAT KARL ALREADY KNOWS unless the conversation changes it.

TASK: REPLY. Write KARL's next WhatsApp message as JSON {{"reply": "..."}}:
{bullets(p['voice'])}
- Answer the guest's newest questions first, from FACTS only; if FACTS don't cover it, say plainly that detail isn't in our listing. Then the next step.
- Restate what you understood (town, dates, party, house) in a few words when it is new, so the guest can correct you. If the dates were assumed, ask the guest to confirm them.
- Keep every KCER number, amount, date and payment detail from MUST GET ACROSS exactly.
- Never copy the guest's sentence back, never repeat one of your earlier messages word for word.
{bullets(p['policies'][2:6])}
Never:
{bullets(p['never'])}"""


SYSTEM = _system()


def _clean_date(v, today: date) -> Optional[date]:
    try:
        d = date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None
    return d if today <= d <= today + timedelta(days=550) else None


def _short(iso: Optional[str]) -> str:
    try:
        d = date.fromisoformat(iso)
        return f"{d.day} {d.strftime('%b')}"
    except (TypeError, ValueError):
        return ""


def label(m: dict) -> str:
    from .gate import is_davis
    if m.get("direction") == "in":
        return "GUEST"
    return "KARL" if is_davis(m.get("sender")) else "TEAM"


def _body(m: dict) -> Optional[str]:
    """One message as the transcript shows it, or None for pure noise."""
    from ..wa_clean import is_system_notice
    from .gate import speaking
    kind, text = m.get("kind") or "text", " ".join((m.get("text") or "").split())
    if kind == "image":
        return "[photos]"
    if kind == "document":
        return f"[PDF {text[:60]}]" if text else "[PDF]"
    if not speaking(m) or not text or is_system_notice(text) or "version of whatsapp web doesn" in text.lower():
        return None
    low = text.lower()
    if low in ("you deleted this message", "this message was deleted"):
        return None
    card_sender = (m.get("sender") or "") in (config.SENDER_CATALOGUE, "Davis (catalogue)")
    if "kcer" in low and ("📅" in text or "💵" in text or card_sender):
        ref = nlu.parse_option(low).get("option_ref")
        return f"[house card KCER {ref}: {text[:160]}]" if ref else f"[house card: {text[:160]}]"
    if card_sender:
        return f"[house card: {text[:160]}]"
    return text[:config.KARL_LINE_CHARS]


def window_start(n: int, offset: int = 0, limit: int = 0) -> int:
    """Where the transcript starts in `messages` (n of them; `offset` = how
    many older messages the chat has before them). The start moves in steps
    of 8 messages, not one per message, so consecutive turns share the same
    opening and llama.cpp's cache reuses it (the prompt grows append-only)."""
    keep = limit or config.KARL_TRANSCRIPT
    start_abs = max(0, offset + n - keep)
    start_abs -= start_abs % 8
    return max(0, start_abs - offset)


def transcript(messages: list[dict], limit: int = 0, tz_minutes: int = 0, offset: int = 0) -> list[str]:
    """The conversation as lines, oldest first, noise removed: WhatsApp
    notices and deleted messages dropped, photo bursts collapsed, house cards
    labelled as ours (so their dates aren't read as the guest's)."""
    out: list[str] = []
    last = None
    for m in messages[window_start(len(messages), offset, limit):]:
        body = _body(m)
        if body is None:
            continue
        when = ""
        try:
            ts = datetime.fromisoformat(str(m.get("created_at")).replace("Z", "+00:00"))
            when = (ts + timedelta(minutes=tz_minutes)).strftime("%d %b %H:%M")
        except (TypeError, ValueError):
            pass
        line = f"{label(m)}: {body}"
        if line == last:
            continue
        out.append(f"[{when}] {line}" if when else line)
        last = line
    return out


def _known(slots: dict) -> str:
    keep = {k: v for k, v in (slots or {}).items() if k in nlu.SLOT_KEYS and v not in (None, "")}
    if keep.get("id_number"):
        keep["id_number"] = "(given)"
    return json.dumps(keep, ensure_ascii=False) if keep else "nothing yet"


def _offer_line(o: dict) -> str:
    bits = [o.get("ref") or "", o.get("place") or "", o.get("capacity") or ""]
    if o.get("check_in"):
        bits.append(f"free {_short(o['check_in'])}→{_short(o.get('check_out'))}")
    if o.get("usd_per_night"):
        bits.append(f"USD {o['usd_per_night']:g}/night")
    return " · ".join(b for b in bits if b)


def shared_context(messages: list[dict], slots: dict, offers: list[dict], today: date, tz_minutes: int = 0,
                   offset: int = 0) -> str:
    """The opening of every request for this turn, identical for STATE and
    REPLY so llama.cpp evaluates it once. The conversation comes first: it
    only grows from one turn to the next, so the next turn re-reads just the
    new messages. What changes every turn (date, state, houses) follows it."""
    lines = transcript(messages, tz_minutes=tz_minutes, offset=offset)
    sent = "\n".join("- " + _offer_line(o) for o in offers[:8]) or "none"
    return ("CONVERSATION (oldest first):\n" + "\n".join(lines) + "\n\n"
            f"TODAY: {today.isoformat()} ({today.strftime('%A')})\n"
            f"WHAT KARL ALREADY KNOWS: {_known(slots)}\n"
            f"HOUSES SENT TO THIS GUEST:\n{sent}")


def understand_messages(messages: list[dict], slots: dict, offers: list[dict], today: date,
                        tz_minutes: int = 0, offset: int = 0) -> list[dict]:
    user = shared_context(messages, slots, offers, today, tz_minutes, offset) + \
        "\n\n=== TASK: STATE ===\nWrite the JSON state for this conversation."
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


# ── when the model is needed (the cascade) ───────────────────────────────────

_REQUEST = re.compile(r"\?|\b(?:pics?|pictures?|photos?|images?|amenit|pool|wifi|parking|where|what|which|how|when|"
                      r"why|can|could|is there|are there|do you|does|send|show|rent|month|deposit|located|location|"
                      r"near|close to|town|sorry|wrong|change|instead|actually|not)\b", re.I)
_MONTHY = re.compile(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*|\d", re.I)


def needs_model(texts: list[str], feats: dict, stage: str) -> Optional[str]:
    """Why this turn needs the model, or None when the rules account for it.
    Simple turns ("ok", "thanks", "KCER 105", a clean "Harare 21-22 Nov for
    4", a bare name or ID when one was asked for) skip the model entirely."""
    text = " ".join(texts).strip()
    if not text:
        return None
    words = len(text.split())
    if feats.get("ack_only") or (words <= 3 and (feats.get("greeting") or feats.get("thanks")) and "?" not in text):
        return None
    if feats.get("option_ref") and words <= 4 and "?" not in text:
        return None
    if stage == "details" and (feats.get("name_like") or feats.get("id_number")) and words <= 8:
        return None
    if _REQUEST.search(text):
        return "question_or_request"
    if _MONTHY.search(text) and not (feats.get("check_in") or feats.get("guests") or feats.get("budget_usd")
                                    or feats.get("option_ref") or feats.get("id_number") or feats.get("bedrooms")):
        return "numbers_the_rules_missed"
    if feats.get("check_in") and not feats.get("check_out"):
        return "half_a_date"
    if words > 12:
        return "long_message"
    if not any(feats.get(k) for k in ("city", "check_in", "guests", "option_ref", "full_name", "name_like",
                                       "greeting", "thanks", "affirm", "negative")):
        return "nothing_recognised"
    return None


# ── the model's reading, checked ─────────────────────────────────────────────

def _city(name: str, places: dict) -> Optional[str]:
    """A town name as the model wrote it, mapped to a known city: exact, an
    alias or suburb, else the closest spelling ("mountplesent" -> mount
    pleasant -> harare)."""
    import difflib
    name = (name or "").strip().lower()
    if not name:
        return None
    hit = nlu.parse_city(name, places)
    if hit:
        return hit
    table = {c.lower(): c.lower() for c in nlu.CITIES}
    table.update({k: v for k, v in nlu.PLACE_ALIASES.items() if v})
    table.update({k.lower(): (v or "").lower() for k, v in (places or {}).items() if v})
    close = difflib.get_close_matches(name, list(table), n=1, cutoff=0.8)
    return table[close[0]] if close else name


def _rules_cross_check(f: dict, guest_texts: list[str], today: date, places: dict) -> None:
    """Explicit facts in the guest's own words beat the model's guesses: the
    last stated party size ("15guests") and, when the model's town is
    unknown, the last town the rules can read."""
    said_guests = said_city = said_nights = None
    words = []
    for t in guest_texts:
        low = " ".join((t or "").lower().split())
        words += re.findall(r"[a-z]{4,}", low)
        g = nlu.parse_guests(low)
        if g:
            said_guests = g
        c = nlu.parse_city(low, places)
        if c:
            said_city = c
        n = nlu.parse_dates(low, today).get("nights")
        if n:
            said_nights = n
    if said_guests and 1 <= said_guests <= 60:
        f["guests"] = said_guests
    blob = " ".join(" ".join((t or "").lower().split()) for t in guest_texts)
    if f.get("check_in"):
        # Dates the guest never wrote are invented: each day number must be in their own words.
        days = {int(d[8:10]) for d in (f.get("check_in"), f.get("check_out")) if d}
        if not all(re.search(rf"(?<!\d)0?{d}(?!\d)", blob) for d in days):
            for k in ("check_in", "check_out", "nights", "dates_assumed"):
                f.pop(k, None)
    if any(nlu.extract(t).get("long_term") for t in guest_texts):
        f["long_term"] = True
    if f.get("city") and f["city"] != said_city:
        # A town the guest never wrote (nor anything close to it) is a guess.
        import difflib
        if not difflib.get_close_matches(f["city"], words, n=1, cutoff=0.75):
            f.pop("city")
    if said_city and not f.get("city"):
        f["city"] = said_city
    if f.get("check_in") and not f.get("check_out") and said_nights and said_nights <= 30:
        co = date.fromisoformat(f["check_in"]) + timedelta(days=said_nights)
        f["check_out"], f["nights"] = co.isoformat(), said_nights


def to_features(u: dict, today: date, places: dict, offers: list[dict], month_hint: Optional[int] = None,
                guest_texts: Optional[list[str]] = None) -> dict:
    """The model's reading, checked, as features for policy.plan. Anything
    that fails a check is dropped (the rules' reading and the stored state
    still apply), so a wrong model answer can't book wrong dates or a house
    we never sent. Dates: the guest's own words (dates_text) through the
    calendar code win when they give both dates; else the model's ISO dates."""
    f: dict = {"understood": True}
    if not isinstance(u, dict):
        return f
    city = _city(u.get("city") or "", places)
    if city:
        f["city"] = city
    dt = str(u.get("dates_text") or "")
    hint = month_hint or _month_for_days(dt, today)
    parsed = nlu.parse_dates(dt, today, hint) if dt else {}
    # The guest's own words through the calendar code first; the model's ISO
    # dates only fill what those words don't give.
    ci = _clean_date(parsed.get("check_in"), today)
    co = _clean_date(parsed.get("check_out"), today) if ci else None
    if not ci:
        ci, co = _clean_date(u.get("check_in"), today), _clean_date(u.get("check_out"), today)
    elif not co:
        model_co = _clean_date(u.get("check_out"), today)
        co = model_co if model_co and ci < model_co <= ci + timedelta(days=30) else None
    if ci:
        f["check_in"] = ci.isoformat()
        if co and ci < co <= ci + timedelta(days=90):
            f["check_out"] = co.isoformat()
            f["nights"] = (co - ci).days
    f["dates_assumed"] = bool(ci) and bool(dt) and not _MONTHY.search(re.sub(r"\d", "", dt)) and month_hint is None
    for k, lo, hi in (("guests", 1, 60), ("bedrooms", 1, 20)):
        v = u.get(k)
        if isinstance(v, int) and lo <= v <= hi:
            f[k] = v
    b = u.get("budget_usd")
    if isinstance(b, (int, float)) and 0 < b < 100000:
        f["budget_usd"] = float(b)
        f["budget_basis"] = u.get("budget_basis") if u.get("budget_basis") in ("night", "total") else None
    f["long_term"] = u.get("stay_type") == "long_term_rental" or u.get("budget_basis") == "month"
    ref = u.get("chosen_ref")
    if isinstance(ref, int) and ref > 0:
        f["option_ref"] = ref
        f["explicit_choice"] = True
    photos = [r for r in (u.get("photos_for") or []) if isinstance(r, int) and r > 0]
    if photos:
        f["photos_for"] = photos[:3]
    if "photos" in (u.get("intents") or []):
        f["photos_request"] = True
    name = " ".join(str(u.get("full_name") or "").split())
    if 2 <= len(name.split()) <= 5 and all(w.replace("-", "").replace("'", "").isalpha() for w in name.split()):
        f["full_name"] = name.title() if name.islower() or name.isupper() else name
    ident = str(u.get("id_number") or "").strip()
    if 5 <= len(ident) <= 25 and any(c.isdigit() for c in ident):
        f["id_number"] = nlu.parse_id(ident) or ident.upper()
    qs = [" ".join(str(q).split())[:160] for q in (u.get("open_questions") or []) if str(q).strip()]
    f["open_questions"] = qs[-4:]
    if qs:
        f["question"] = True
    intents = set(u.get("intents") or [])
    for intent, key in (("price", "price_query"), ("location", "location_query"), ("invoice", "invoice_request"),
                        ("payment_proof", "payment_claim"), ("terms", "terms_request"), ("complaint", "complaint"),
                        ("human", "human_request"), ("opt_out", "opt_out"), ("greeting", "greeting"),
                        ("thanks", "thanks")):
        if intent in intents:
            f[key] = True
    if qs:
        f["latest_need"] = qs[-1]
    if guest_texts:
        _rules_cross_check(f, guest_texts, today, places)
    return f


def _month_for_days(text: str, today: date) -> Optional[int]:
    """A day range with no month ("From 23 to 25"): this month if the first
    day is still ahead, else next month. The reply then asks the guest to
    confirm (dates_assumed)."""
    if not text or _MONTHY.search(re.sub(r"\d", "", text)) or re.search(r"\d{1,2}\s*[./\-]\s*\d{1,2}", text):
        return None                          # a month is named, or written as a number (22.3.27)
    days = [int(d) for d in re.findall(r"(?<!\d)(\d{1,2})(?!\d)", text) if 1 <= int(d) <= 31]
    if not days:
        return None
    return today.month if days[0] >= today.day else (today.month % 12) + 1


# Features the model's reading overrides; everything else it adds to (the
# rules' own reading of the newest messages still counts: OR for flags).
_OVERRIDE = ("city", "check_in", "check_out", "nights", "guests", "bedrooms", "budget_usd", "budget_basis",
             "full_name", "id_number", "option_ref")


def merge(regex_feats: dict, model_feats: dict) -> dict:
    out = dict(regex_feats)
    for k, v in model_feats.items():
        if k in _OVERRIDE:
            if v not in (None, ""):
                out[k] = v
        elif isinstance(v, bool):
            out[k] = out.get(k, False) or v
        elif v not in (None, "", []):
            out[k] = v
    if model_feats.get("check_in") and not model_feats.get("check_out"):
        out.pop("check_out", None)           # the model saw a new start date without an end
    return out


# ── the reply ────────────────────────────────────────────────────────────────

def reply_messages(chat_name: str, messages: list[dict], plan: dict, feats: dict, today: date,
                   max_words: int, feedback: str = "", tz_minutes: int = 0,
                   slots_before: Optional[dict] = None, offers: Optional[list] = None, offset: int = 0) -> list[dict]:
    """REPLY: the same opening as STATE (so it is not evaluated again), then
    the facts, what the system will do, the points to carry, and last the
    guest's newest messages and open questions."""
    from .prompts import _ACTION_TEXT, facts_text, must_include
    draft = next((a["text"] for a in plan["actions"] if a["type"] == "reply"), "")
    # The rules' "that detail isn't in our listing" stands in for an answer
    # they can't give; the model answers from FACTS itself.
    draft = draft.replace(persona.line("no_detail", next="").strip(), "").strip()
    doing = [_ACTION_TEXT[a["type"]] for a in plan["actions"] if a["type"] in _ACTION_TEXT]
    mine = [m.get("text") for m in messages if m.get("sender") in (config.SENDER, "Davis")
            and (m.get("kind") or "text") == "text"][-3:]
    newest, seen_out = [], False
    for m in reversed(messages):
        if m.get("direction") == "in" and (m.get("kind") or "text") == "text" and m.get("text"):
            if seen_out:
                break
            newest.append(" ".join(m["text"].split())[:300])
        elif m.get("direction") == "out" and (m.get("kind") or "text") == "text" and m.get("text"):
            seen_out = bool(newest)
    newest.reverse()
    questions = feats.get("open_questions") or []
    must = must_include(draft)
    task = [f"=== TASK: REPLY === (guest: {chat_name}, stage: {plan['stage']})",
            f"FACTS:\n{facts_text(plan['facts'])}",
            "YOUR LAST MESSAGES (don't repeat): " + (" | ".join(t for t in mine if t) or "none"),
            "THE SYSTEM WILL, right after your reply: " + ("; ".join(doing) if doing else "nothing else"),
            f"MUST GET ACROSS: {draft}"]
    if must:
        task.append("KEEP EXACTLY: " + " | ".join(must))
    if feats.get("dates_assumed") and (plan.get("slots") or {}).get("check_in"):
        task.append("The month of the dates was assumed: say which dates you took and ask the guest to confirm.")
    if feedback:
        task.append(f"YOUR LAST TRY WAS REJECTED ({feedback}). Fix that.")
    task.append("GUEST'S NEWEST MESSAGES:\n" + ("\n".join(f"> {t}" for t in newest) or "> (none: a follow-up or "
                                                                                       "an update from the system)"))
    if questions:
        task.append("ANSWER FIRST:\n" + "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1)))
    task.append(f'Write KARL\'s reply as JSON {{"reply": "..."}}, at most {max_words} words.')
    opening = shared_context(messages, slots_before if slots_before is not None else (plan.get("slots") or {}),
                             offers if offers is not None else [], today, tz_minutes, offset)
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": opening + "\n\n" + "\n".join(task)}]
