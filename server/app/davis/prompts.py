"""davis/prompts.py — what the local model is told. The system prompt (the
persona) never changes between requests, so the model keeps its computed
prefix and only reads each turn's new text; the turn itself is kept short
(recent messages, facts as one line per house), because on a CPU every
prompt token costs time."""

from __future__ import annotations

from . import config, persona
from .catalogue import fmt_range

SYSTEM = persona.system_prompt(config.MAX_WORDS)

# Only what the model must decide: the wording, and whether a person is
# needed. Slots come from nlu.py; a small schema keeps the answer short
# (every generated token is ~0.1 s on a 2-core CPU).
SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "handoff": {"type": "boolean"},
    },
    "required": ["reply", "handoff"],
    "additionalProperties": False,
}

_ACTION_TEXT = {
    "send_options": "send the houses under SENDING NOW as cards with photos, right after your reply",
    "send_terms": "send the Terms & Conditions PDF",
    "create_invoice": "create and send the invoice PDF",
    "request_refresh": "check live prices for the guest's dates (a few minutes)",
    "alert_staff": "alert a team member (you keep helping the guest)",
    "maybe_alert": "alert a team member unless FACTS answer the question",
    "opt_out": "stop messaging this guest",
    "opt_in": "resume messaging this guest",
}


def _money(v) -> str:
    return f"USD {v:,.2f}".replace(".00", "") if v else ""


def house_line(o: dict) -> str:
    """One house as one line of facts (never its link)."""
    parts = [o.get("ref") or "", o.get("place") or "", o.get("capacity") or ""]
    when = fmt_range(o.get("check_in"), o.get("check_out"))
    if when:
        parts.append(when + (f" ({o['nights']} nights)" if o.get("nights") else ""))
    if o.get("usd_per_night"):
        parts.append(f"{_money(o['usd_per_night'])}/night")
    if o.get("usd_total"):
        parts.append(f"{_money(o['usd_total'])} total")
    if o.get("rating"):
        parts.append(f"rating {o['rating']}")
    if o.get("note"):
        parts.append(f"note: {o['note']}")
    return " · ".join(p for p in parts if p)


def facts_text(facts: dict, focus: bool = False) -> str:
    """The facts as short lines. focus=True (an open question): only the
    houses that matter for it, so the CPU reads fewer tokens."""
    s = facts.get("slots") or {}
    frame = [f"{k}={s[k]}" for k in ("city", "check_in", "check_out", "nights", "guests", "budget_usd", "full_name")
             if s.get(k) not in (None, "")]
    if s.get("id_number"):
        frame.append("id=provided")
    out = ["BOOKING: " + (", ".join(frame) or "nothing yet"),
           f"PAYMENT: {persona.ECOCASH} (the only payment number you may give)"]
    if facts.get("choice"):
        out.append("CHOSEN: " + house_line(facts["choice"]))
    if facts.get("asked_about"):
        out.append("ASKED ABOUT: " + house_line(facts["asked_about"]))
    if facts.get("sending_now"):
        out.append("SENDING NOW:\n" + "\n".join("- " + house_line(o) for o in facts["sending_now"]))
    before = facts.get("offered_before") or []
    if focus and (facts.get("asked_about") or facts.get("choice")):
        before = []                                  # the house in question is already listed above
    elif focus:
        before = [o for o in before if o.get("note")][:3] or before[:3]
    if before:
        out.append("SENT BEFORE:\n" + "\n".join("- " + house_line(o) for o in before[:4]))
    return "\n".join(out)


def _who(m: dict) -> str:
    if m.get("direction") == "in":
        return "GUEST"
    return "DAVIS" if (m.get("sender") or "").startswith("Davis") else "TEAM"


def user_turn(chat_name: str, history: list[dict], plan: dict, today: str) -> str:
    focus = plan.get("intent") == "open_question"
    lines, last = [], None
    for m in history[-(4 if focus else config.MODEL_HISTORY):]:
        if m.get("kind") == "image":
            body = "[photos]"
        elif m.get("kind") == "document":
            body = f"[PDF: {(m.get('text') or '')[:60]}]"
        elif (m.get("sender") or "") == config.SENDER_CATALOGUE:
            body = "[house card]"
        else:
            body = " ".join((m.get("text") or "").split())[:160 if focus else 220]
        line = f"{_who(m)}: {body}"
        if line != last:                       # 9 photos read as one line
            lines.append(line)
        last = line
    draft = next((a["text"] for a in plan["actions"] if a["type"] == "reply"), "")
    doing = [_ACTION_TEXT[a["type"]] for a in plan["actions"] if a["type"] in _ACTION_TEXT]
    return (
        f"TODAY {today} · GUEST {chat_name} · STAGE {plan['stage']}\n"
        f"CHAT:\n" + "\n".join(lines) + "\n"
        f"FACTS:\n{facts_text(plan['facts'], focus)}\n"
        f"SYSTEM WILL: " + ("; ".join(doing) if doing else "send your reply only") + "\n"
        f"GOAL: {plan['goal']}\n"
        f"DRAFT: {draft}\n"
        f"Write DAVIS's next WhatsApp message to the guest."
    )


def messages(user: str) -> list[dict]:
    """System (persona) + the worked examples as earlier turns + this turn.
    Everything before the last message is identical on every call, so the
    local model reuses its computed prefix."""
    out = [{"role": "system", "content": SYSTEM}]
    for ex_user, ex_answer in persona.EXAMPLES:
        out += [{"role": "user", "content": ex_user}, {"role": "assistant", "content": ex_answer}]
    return out + [{"role": "user", "content": user}]
