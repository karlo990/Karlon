"""davis/mind.py — Karl's mind: how Karl understands a conversation and how it
is asked to answer. Used when the provider is "claude".

Why this exists. Reading the real chats (Oct 2026) showed the failures were
not in the wording but in understanding: dates written "4november",
"22.3.27", "From 23 to 25" were missed and asked for again; a name sent on
its own line was not kept; dates on our own house cards were taken as the
guest's; questions about photos, pools and bedrooms were ignored; a guest
who wanted a monthly room was sent holiday homes. Rules can't keep up with
how people write. So Karl now has two steps per guest turn, each one model
call with a fixed JSON schema:

1. understand(): the model reads the whole recent conversation (not only the
   last message) and writes the booking state: city, dates, party, budget,
   chosen house, name, ID, stay type, photo requests, and the guest's
   questions that are still unanswered. This is dialogue state tracking done
   by a language model (ParsingDST, Wu et al., Findings of EMNLP 2023; Heck
   et al., 2023), checked by code before anything uses it (dates must be
   real and ahead, the house must be one we sent, and so on).
2. The reply (engine._phrase): the model writes Karl's message from the same
   conversation, the checked facts, what the system is doing (houses,
   invoice) and the key points the booking needs. The guest's newest
   messages and open questions go LAST in the prompt: models use the start
   and end of a long input best (Liu et al., "Lost in the Middle", TACL
   2024).

What the code still decides. Which houses go out, prices, the invoice, the
Terms PDF and live-price requests stay with policy.py and catalogue.py, and
every reply passes policy.check_reply (no invented price, house or number).
The model understands and speaks; code acts. This keeps a booking correct
even when a model is wrong.

The communication model. Karl is designed around Shannon & Weaver (1949),
with the feedback loop Schramm (1954) added to it:
  source       the guest; the message is spread over several WhatsApp lines
  noise        typos, split messages, deleted messages, system notices,
               our own house cards, staff side-remarks: removed or labelled
               in the transcript (transcript()), not guessed at
  receiver     understand(): decodes the whole turn into a state
  destination  policy.plan(): what the booking needs next
  feedback     Karl restates what it understood (place, dates, party) so the
               guest can correct it, and asks one clear question when
               something is ambiguous instead of guessing (grounding, Clark &
               Brennan 1991)
  channel      a phone screen: short messages, never a repeated line.

Memory. The conversation cache is three layers: the recent transcript itself
(KARL_TRANSCRIPT messages, read every turn), the booking state stored in
davis_sessions (survives between turns), and the houses already sent to
this guest (house_sends). Nothing is summarised away: the model reads the
real messages, so the most recent question is always answered in context.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Optional

from . import config, nlu, persona


def _null(t: dict) -> dict:
    return {"anyOf": [t, {"type": "null"}]}


_STR, _INT, _NUM, _BOOL = {"type": "string"}, {"type": "integer"}, {"type": "number"}, {"type": "boolean"}

INTENTS = ["greeting", "small_talk", "booking", "price", "availability", "photos", "amenities", "location",
           "invoice", "payment_proof", "deposit", "terms", "complaint", "human", "opt_out", "thanks", "other"]

UNDERSTAND_SCHEMA = {
    "type": "object",
    "properties": {
        "latest_need": _STR,
        "city": _null(_STR),
        "areas": {"type": "array", "items": _STR},
        "check_in": _null(_STR),
        "check_out": _null(_STR),
        "dates_assumed": _BOOL,
        "guests": _null(_INT),
        "bedrooms": _null(_INT),
        "budget_usd": _null(_NUM),
        "budget_basis": _null({"type": "string", "enum": ["night", "total", "month"]}),
        "stay_type": {"type": "string", "enum": ["short_stay", "long_term_rental", "unknown"]},
        "chosen_ref": _null(_INT),
        "photos_for": {"type": "array", "items": _INT},
        "wants_more_options": _BOOL,
        "full_name": _null(_STR),
        "id_number": _null(_STR),
        "open_questions": {"type": "array", "items": _STR},
        "intents": {"type": "array", "items": {"type": "string", "enum": INTENTS}},
        "mood": {"type": "string", "enum": ["neutral", "happy", "impatient", "upset"]},
    },
    "required": ["latest_need", "city", "areas", "check_in", "check_out", "dates_assumed", "guests", "bedrooms",
                 "budget_usd", "budget_basis", "stay_type", "chosen_ref", "photos_for", "wants_more_options",
                 "full_name", "id_number", "open_questions", "intents", "mood"],
    "additionalProperties": False,
}

UNDERSTAND_SYSTEM = f"""You read a WhatsApp conversation between a guest and {persona.BRAND}, which rents furnished short-stay homes in Zimbabwe (Harare, Bulawayo, Victoria Falls, Marondera, Rusape and other towns), and you write down what the guest wants as JSON. You do not reply to the guest.

Who is who in the transcript: GUEST is the customer. KARL is our booking assistant. TEAM is our staff. [house card KCER n ...] is a house we sent, with the dates and price it is free for. [photos], [PDF] and [deleted] are attachments and notices.

How to read it:
- Read the WHOLE conversation. Guests split one thought over several messages, answer questions late, and correct themselves. The newest statement wins.
- Take details from the guest's own words. A detail a KARL or TEAM message proposed counts only if the guest agreed to it ("ok", "yes", picking it).
- Dates on house cards ("📅 Free 18 Oct → 20 Oct") are when that house is free. They are NOT the guest's dates unless the guest picked that house for those dates.
- Dates: write YYYY-MM-DD. Day comes before month (22.3.27 is 22 March 2027; 4november is 4 November). A date without a year is the next one on or after TODAY. A day range without a month ("from 23 to 25") uses the month named most recently in the conversation; if none, the nearest month in which those days are still ahead, and set dates_assumed true. One night "check in 21, check out 22" means check_in 21 and check_out 22. If only a start and a number of nights is given, compute check_out. If the guest corrected a date ("sorry wrong date"), use the corrected one. Obvious typos in a year (23 3.37 after 22.3.27) mean the same year as the other date.
- guests: the number of people staying (2 couples = 4). bedrooms: only if the guest asked for a number of bedrooms or rooms.
- city: the town they want to stay in, lowercase. Suburbs belong to their city (Avondale, Borrowdale, Mount Pleasant are Harare; Hillside is Bulawayo); put the suburbs they named in areas.
- budget_usd: a number only if the guest gave a budget; budget_basis "month" for a monthly rent.
- stay_type: long_term_rental when they want to rent monthly or long term (a room to rent, rent per month, lease, "not booking"); short_stay for nights or holidays; unknown if not clear yet.
- chosen_ref: the KCER number the guest picked to book. Only a house that was sent or that they named. Not a house they only asked about.
- photos_for: KCER numbers the guest wants pictures of. "pics" about the house just discussed means that house.
- full_name / id_number: only what the guest gave for the invoice (a full name on its own line after we asked for it counts). Never invent them.
- open_questions: every question the guest asked that the conversation has NOT answered yet, in plain words, newest last. A later KARL or TEAM message that answered it removes it. Requests count as questions ("send pics", "how much deposit", "is there a pool", "where is it located").
- latest_need: one short sentence: what the guest wants from us right now.
- WHAT KARL ALREADY KNOWS is the state from before; keep those values unless the conversation changes them, and fill what is missing.
Return only the JSON."""


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
        return "[deleted]"
    if "kcer" in low and ("📅" in text or "💵" in text or (m.get("sender") or "") in (config.SENDER_CATALOGUE,
                                                                                    "Davis (catalogue)")):
        ref = nlu.parse_option(low).get("option_ref")
        return f"[house card KCER {ref}: {text[:200]}]" if ref else f"[house card: {text[:200]}]"
    if (m.get("sender") or "") in (config.SENDER_CATALOGUE, "Davis (catalogue)"):
        return f"[house card: {text[:200]}]"
    return text[:600]


def transcript(messages: list[dict], limit: int = 0, tz_minutes: int = 0) -> list[str]:
    """The conversation as numbered lines, oldest first, noise removed:
    WhatsApp system notices dropped, photo bursts collapsed, house cards
    labelled as ours (so their dates aren't read as the guest's)."""
    out: list[str] = []
    last = None
    for m in messages[-(limit or config.KARL_TRANSCRIPT):]:
        body = _body(m)
        if body is None:
            continue
        when = ""
        try:
            ts = datetime.fromisoformat(str(m.get("created_at")).replace("Z", "+00:00"))
            when = (ts + timedelta(minutes=tz_minutes)).strftime("%d %b %H:%M") + " "
        except (TypeError, ValueError):
            pass
        line = f"{label(m)}: {body}"
        if line == last:
            continue
        out.append(f"[{when.strip()}] {line}" if when else line)
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
        bits.append(f"{_short(o['check_in'])}→{_short(o.get('check_out'))}")
    if o.get("usd_per_night"):
        bits.append(f"USD {o['usd_per_night']:g}/night")
    return " · ".join(b for b in bits if b)


def understand_messages(messages: list[dict], slots: dict, offers: list[dict], today: date,
                        tz_minutes: int = 0) -> list[dict]:
    lines = transcript(messages, tz_minutes=tz_minutes)
    sent = "\n".join("- " + _offer_line(o) for o in offers[:12]) or "none"
    user = (f"TODAY: {today.isoformat()} ({today.strftime('%A')})\n"
            f"WHAT KARL ALREADY KNOWS: {_known(slots)}\n"
            f"HOUSES SENT TO THIS GUEST:\n{sent}\n"
            f"CONVERSATION (oldest first; the guest's newest messages are the last GUEST lines):\n"
            + "\n".join(lines) + "\n\nWrite the JSON for this conversation.")
    return [{"role": "system", "content": UNDERSTAND_SYSTEM}, {"role": "user", "content": user}]


def to_features(u: dict, today: date, places: dict, offers: list[dict]) -> dict:
    """The model's reading, checked, as features for policy.plan. Anything
    that fails a check is dropped (the regex reading and the stored state
    still apply), so a wrong model answer can't book wrong dates or a house
    we never sent."""
    f: dict = {"understood": True}
    if not isinstance(u, dict):
        return f
    city = (u.get("city") or "").strip().lower()
    if city:
        f["city"] = nlu.parse_city(city, places) or city
    ci, co = _clean_date(u.get("check_in"), today), _clean_date(u.get("check_out"), today)
    if ci:
        f["check_in"] = ci.isoformat()
        if co and ci < co <= ci + timedelta(days=90):
            f["check_out"] = co.isoformat()
            f["nights"] = (co - ci).days
    f["dates_assumed"] = bool(u.get("dates_assumed")) and bool(ci)
    for k, lo, hi in (("guests", 1, 60), ("bedrooms", 1, 20)):
        v = u.get(k)
        if isinstance(v, int) and lo <= v <= hi:
            f[k] = v
    b = u.get("budget_usd")
    if isinstance(b, (int, float)) and 0 < b < 100000:
        f["budget_usd"] = float(b)
        f["budget_basis"] = u.get("budget_basis") if u.get("budget_basis") in ("night", "total") else None
    f["long_term"] = u.get("stay_type") == "long_term_rental" or u.get("budget_basis") == "month"
    sent_refs = {o.get("ref_no") for o in offers if o.get("ref_no") is not None}
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
    qs = [" ".join(str(q).split())[:200] for q in (u.get("open_questions") or []) if str(q).strip()]
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
    if u.get("wants_more_options"):
        f["more_options"] = True
    f["latest_need"] = " ".join(str(u.get("latest_need") or "").split())[:240]
    f["mood"] = u.get("mood") or "neutral"
    f["areas"] = [str(a)[:40] for a in (u.get("areas") or [])][:4]
    f["sent_refs"] = sorted(r for r in sent_refs if r is not None)
    return f


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

def reply_system(max_words: int) -> str:
    p = persona.PERSONA
    bullets = lambda xs: "\n".join(f"- {x}" for x in xs)     # noqa: E731
    voice = [v.replace("40 words", f"{max_words} words") for v in p["voice"]]
    return (
        f"You are {p['name']}, the virtual booking assistant for {persona.BRAND}: {p['role']}. {p['company']}\n\n"
        f"Character:\n{bullets(p['character'])}\n\n"
        f"Voice:\n{bullets(voice)}\n\n"
        f"Selling:\n{bullets(p['selling'])}\n\n"
        f"House policies:\n{bullets(p['policies'])}\n\n"
        f"Never:\n{bullets(p['never'])}\n\n"
        "How you communicate (Shannon and Weaver's model, with Schramm's feedback loop):\n"
        "- The guest's message is spread over their recent lines. Read the whole conversation and answer what they "
        "mean, not only the last line.\n"
        "- Noise: typos, split messages, our own earlier mistakes. Understand through it. Never copy the guest's "
        "sentence back to them, and never repeat one of your earlier messages word for word.\n"
        "- Feedback: when you understood something new (town, dates, party size, the house), restate it in a few "
        "words so the guest can correct you. If the dates were assumed, say which ones you took and ask them to "
        "confirm. When something important is ambiguous, ask one clear question instead of guessing.\n"
        "- Channel: one WhatsApp message on a phone. Short. Answer their questions first, then the one next step.\n\n"
        "Each turn you get the booking state, FACTS (the only facts you may state), the conversation, what the "
        "system is about to do with your reply (send houses, the invoice, the Terms PDF, check live prices), the "
        "points this message must get across, and last, the guest's newest messages with their unanswered "
        "questions. Answer every unanswered question you can from FACTS; if FACTS don't cover it, say plainly that "
        "detail isn't in our listing (never 'I'll check with the team'). Keep every KCER number, amount, date, "
        "time and payment detail from the points exactly as given. Write as KARL, to the guest.\n"
        'Answer with JSON only: {"reply": "..."}'
    )


REPLY_SCHEMA = {"type": "object", "properties": {"reply": _STR}, "required": ["reply"],
                "additionalProperties": False}


def reply_messages(chat_name: str, messages: list[dict], plan: dict, feats: dict, today: date,
                   max_words: int, feedback: str = "", tz_minutes: int = 0) -> list[dict]:
    from .prompts import _ACTION_TEXT, facts_text, must_include
    lines = transcript(messages, tz_minutes=tz_minutes)
    draft = next((a["text"] for a in plan["actions"] if a["type"] == "reply"), "")
    # The rules' "that detail isn't in our listing" is their stand-in for an
    # answer they can't give; the model answers from FACTS itself.
    draft = draft.replace(persona.line("no_detail", next="").strip(), "").strip()
    doing = [_ACTION_TEXT[a["type"]] for a in plan["actions"] if a["type"] in _ACTION_TEXT]
    mine = [m.get("text") for m in messages if label(m) == "KARL" and (m.get("kind") or "text") == "text"
            and m.get("sender") in (config.SENDER, "Davis")][-4:]
    newest, seen_out = [], False
    for m in reversed(messages):
        if m.get("direction") == "in" and (m.get("kind") or "text") == "text" and m.get("text"):
            if seen_out:
                break
            newest.append(" ".join(m["text"].split())[:400])
        elif m.get("direction") == "out" and (m.get("kind") or "text") == "text" and m.get("text"):
            seen_out = bool(newest)
    newest.reverse()
    questions = feats.get("open_questions") or []
    must = must_include(draft)
    parts = [
        f"TODAY {today.isoformat()} · GUEST {chat_name} · STAGE {plan['stage']}",
        f"FACTS:\n{facts_text(plan['facts'])}",
        "CONVERSATION (oldest first):\n" + "\n".join(lines),
        "YOUR RECENT MESSAGES (don't repeat any of them word for word):\n"
        + ("\n".join(f"- {t}" for t in mine) if mine else "- none"),
        "THE SYSTEM WILL, right after your reply: " + ("; ".join(doing) if doing else "nothing else"),
        f"THIS MESSAGE MUST GET ACROSS (the booking's next step): {draft}",
    ]
    if must:
        parts.append("KEEP EXACTLY: " + " | ".join(must))
    if feats.get("dates_assumed") and (plan.get("slots") or {}).get("check_in"):
        parts.append("THE DATES WERE ASSUMED from an incomplete message: say which dates you took and ask the "
                     "guest to confirm.")
    if feedback:
        parts.append(f"YOUR LAST TRY WAS REJECTED ({feedback}). Write it again and fix that.")
    parts.append("THE GUEST'S NEWEST MESSAGES:\n" + ("\n".join(f"> {t}" for t in newest) or "> (no new message: "
                                                     "this is a follow-up or an update from the system)"))
    if feats.get("latest_need"):
        parts.append(f"WHAT THEY WANT NOW: {feats['latest_need']}")
    if questions:
        parts.append("ANSWER THESE FIRST:\n" + "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1)))
    parts.append(f"Write KARL's next WhatsApp message (at most {max_words} words).")
    return [{"role": "system", "content": reply_system(max_words)}, {"role": "user", "content": "\n\n".join(parts)}]
