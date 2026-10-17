"""davis/policy.py — what Davis does next: a frame-based booking dialogue.

The frame (city, dates, guests, budget, chosen house, name, ID) fills from
the guest's messages; the stage follows from what is filled:

    new → qualify → present → details → invoice → payment (staff confirm)
                  ↘ searching (live prices requested) ↗

Every turn produces a plan: the actions Davis's "hands" perform (send a
reply, send houses with photos, send the Terms PDF, create the invoice, ask
the scraper for live prices, hand the chat to staff) and a template reply.
The model, when one is configured, rewrites only the reply text; the actions
are decided here, with code, so a booking never depends on how a model felt
about it. Replies follow the guest-reception basics that close deals: answer
fast, one clear question per message (Grice's maxim of quantity), restate
what was agreed, always end with the next step.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

from . import config, nlu

STAGES = ("new", "qualify", "searching", "present", "details", "invoice", "payment", "opted_out")


def _city(slots: dict) -> str:
    return (slots.get("city") or "").title()


def _first_name(slots: dict, chat_name: str) -> str:
    if slots.get("full_name"):
        return slots["full_name"].split()[0]
    name = (chat_name or "").strip()
    if re.fullmatch(r"[A-Za-z][A-Za-z'\-]+(?: [A-Za-z][A-Za-z'\-]+){0,2}", name):
        return name.split()[0].title()
    return ""


def _money(v: Optional[float]) -> str:
    if v is None:
        return ""
    return f"USD {v:,.2f}".replace(".00", "")


def _when(ci: Optional[str], co: Optional[str]) -> str:
    from .catalogue import fmt_range
    return fmt_range(ci, co)


def turn_features(texts: list[str], today, places: dict, has_image: bool = False) -> dict:
    """Features of the guest's whole turn (several quick messages read as
    one): booleans OR-ed, slots newest-wins, plus ack_only."""
    agg: dict = {"texts": texts, "has_image": has_image}
    acks = []
    for t in texts:
        f = nlu.extract(t, today, places)
        acks.append(bool(f.get("ack")) or f.get("empty"))
        if any(f.get(k) for k in ("greeting", "thanks", "affirm", "negative", "question")):
            f.pop("name_like", None)          # "Yes please" is not a name; only a plain message can be
        for k, v in f.items():
            if isinstance(v, bool):
                agg[k] = agg.get(k, False) or v
            elif v not in (None, ""):
                agg[k] = v
    agg["ack_only"] = bool(texts) and all(acks) and not has_image
    return agg


def seed_slots(history_texts: list[str], today, places: dict) -> dict:
    """Rebuild the frame from earlier guest messages (after a server restart
    wiped the sessions, the chat history still says what the guest wants).
    Dates already in the past are dropped."""
    slots: dict = {}
    for t in history_texts:
        slots = nlu.merge_slots(slots, nlu.extract(t, today, places))
    if slots.get("check_in") and slots["check_in"] < today.isoformat():
        for k in ("check_in", "check_out", "nights"):
            slots.pop(k, None)
    return slots


class Catalogue:
    """What the policy needs from the live catalogue (catalogue.py in
    production, a fake in tests)."""
    find_options: Callable[[dict, bool], tuple[list[dict], str]]
    resolve_choice: Callable[[dict, list, dict, str], Optional[dict]]


def plan(ctx: dict, feats: dict, cat, trigger: str = "message") -> dict:
    s = ctx["session"]
    stage = s.get("stage") or "new"
    old = dict(s.get("slots") or {})
    slots = nlu.merge_slots(old, {k: v for k, v in feats.items() if k in nlu.SLOT_KEYS and k != "full_name"})
    if feats.get("full_name"):
        slots["full_name"] = feats["full_name"]
    offers = ctx.get("offers") or []
    first = _first_name(slots, ctx.get("chat_name", ""))
    hi = f" {first}" if first else ""
    actions: list[dict] = []
    out = {"stage": stage, "slots": slots, "choice": s.get("choice"), "actions": actions,
           "intent": "", "goal": "", "facts": {}}

    def reply(text: str, goal: str, intent: str) -> None:
        actions.append({"type": "reply", "text": nlu.clip_words(text, config.MAX_WORDS)})
        out["goal"], out["intent"] = goal, intent

    def done(stage_: str) -> dict:
        order = {"reply": 0, "send_options": 1, "create_invoice": 2, "send_terms": 3}
        actions.sort(key=lambda a: order.get(a["type"], 9))         # the reply leads
        out["stage"] = stage_
        out["facts"] = _facts(slots, out.get("choice"), offers, actions)
        return out

    # ── safety first: the guest's own wishes about the conversation ──────────
    if feats.get("opt_in") and s.get("opted_out"):
        actions.append({"type": "opt_in"})
        reply(f"Welcome back{hi}! Which city and dates are you looking at?",
              "welcome the guest back and ask city and dates", "opt_in")
        return done("qualify")
    if feats.get("opt_out"):
        actions.append({"type": "opt_out"})
        reply("Understood, I won't message you again. Reply START anytime you need a stay.",
              "confirm we will stop messaging", "opt_out")
        return done("opted_out")
    if feats.get("complaint") or feats.get("human_request"):
        reason = "complaint" if feats.get("complaint") else "asked for a person"
        actions.append({"type": "handoff", "reason": reason})
        reply(("I'm sorry about this. " if feats.get("complaint") else "Of course. ")
              + "A member of our team will reply to you here shortly.",
              "tell the guest a team member will take over", "handoff")
        return done(stage)
    if feats.get("payment_claim") or (feats.get("has_image") and stage in ("invoice", "payment")):
        actions.append({"type": "handoff", "reason": "payment proof to verify"})
        reply(f"Thank you{hi}! Our team is confirming your payment now and will send your booking "
              "confirmation shortly.", "thank the guest; staff will confirm the payment", "payment")
        return done("payment")

    prefix = ""
    if feats.get("bot_question"):
        hello = f"Hello{hi}! " if stage == "new" or feats.get("greeting") else ""
        prefix = hello + "I'm Davis, KarlCon's virtual booking assistant; a team member can join anytime. "
    if feats.get("terms_request") and not s.get("terms_sent_at"):
        actions.append({"type": "send_terms"})
        prefix += "Our Terms & Conditions are attached 📄 "

    def say(text: str, goal: str, intent: str) -> None:
        reply(prefix + text, goal, intent)

    # ── live prices arrived for a search Davis promised ──────────────────────
    if trigger == "refresh_done" and slots.get("city") and slots.get("check_in") and slots.get("check_out"):
        return _present(ctx, slots, cat, actions, say, done, out, resumed=True)

    # ── a house picked (or asked about) ──────────────────────────────────────
    choice = cat.resolve_choice(feats, offers, slots, stage)
    tentative = bool(choice) and feats.get("question") and not feats.get("explicit_choice") \
        and not feats.get("affirm")
    if not choice and s.get("choice") and stage == "present" and feats.get("affirm"):
        choice = s["choice"]                                  # "yes" to "shall I reserve it?"
    if choice:
        out["choice"] = choice
        if choice.get("location"):
            slots["city"] = choice["location"]
        if choice.get("check_in") and choice.get("check_out"):
            slots["check_in"], slots["check_out"] = choice["check_in"], choice["check_out"]
            slots = nlu.merge_slots(slots, {})
        if tentative and choice.get("usd_per_night"):
            price = _price_line(choice)
            say(f"{choice['ref']} is in {choice.get('place') or _city(slots)}{', ' + price if price else ''}. "
                "Shall I reserve it for you?", "answer about the house and offer to reserve it", "price")
            return done("present")
        stage = "details"
    elif s.get("choice") and stage in ("details", "invoice", "searching"):
        choice = s["choice"]
        new_dates = (slots.get("check_in"), slots.get("check_out")) != (choice.get("check_in"), choice.get("check_out"))
        if new_dates and slots.get("check_in") and slots.get("check_out"):
            choice = cat.resolve_choice({"option_ref": choice.get("ref_no")}, [], slots, stage) or choice
        out["choice"] = choice

    # ── collecting the invoice details ───────────────────────────────────────
    if choice and stage in ("details", "invoice", "searching"):
        if stage == "details" and not slots.get("full_name") and feats.get("name_like"):
            slots["full_name"] = feats["name_like"]
        if feats.get("id_number"):
            slots["id_number"] = feats["id_number"]
        elif stage == "details" and not slots.get("id_number") and feats.get("long_number"):
            slots["id_number"] = feats["long_number"]
        first = _first_name(slots, ctx.get("chat_name", ""))
        hi = f" {first}" if first else ""
        if not choice.get("usd_per_night"):
            if slots.get("check_in") and slots.get("check_out"):
                actions.append({"type": "request_refresh", "city": choice.get("location") or slots.get("city"),
                                "check_in": slots["check_in"], "check_out": slots["check_out"]})
                say(f"Let me confirm {choice['ref']}'s live price for {_when(slots['check_in'], slots['check_out'])}. "
                    "I'll come back to you in a few minutes.", "say live price is being checked", "searching")
                return done("searching")
            say(f"Lovely choice! Which dates would you like {choice['ref']} for, and for how many guests?",
                "ask the dates for the chosen house", "qualify")
            return done("details")
        summary = f"{choice['ref']}, {_when(choice.get('check_in'), choice.get('check_out'))}, " \
                  f"{_money(choice.get('usd_total') or choice.get('usd_per_night'))}" \
                  f"{' total' if choice.get('usd_total') else ' per night'}"
        name, ident = slots.get("full_name"), slots.get("id_number")
        if (not name or not ident) and (feats.get("invoice_request") or feats.get("price_query")):
            prefix += "The invoice shows the total and how to pay. "
        if not name and not ident:
            say(f"Great choice: {summary}. To prepare your invoice, please send your full name and ID or "
                "passport number.", "confirm the choice and ask full name and ID number", "collect_details")
            return done("details")
        if not ident:
            say(f"Thank you{hi}! Please send your ID or passport number for the invoice.",
                "ask the ID or passport number", "collect_details")
            return done("details")
        if not name:
            say("Thank you! And your full name as it should appear on the invoice?",
                "ask the full name", "collect_details")
            return done("details")
        key = f"{choice.get('url')}|{choice.get('check_in')}|{choice.get('check_out')}|{name}|{ident}"
        if key != s.get("invoice_key"):
            actions.append({"type": "create_invoice", "key": key, "invoice": {
                "guest_name": name, "id_number": ident, "location": (choice.get("location") or "").title(),
                "property_name": choice["ref"], "check_in": choice.get("check_in"),
                "check_out": choice.get("check_out"), "rate": choice.get("usd_per_night") or 0.0,
                "guests": int(slots.get("guests") or 1), "chat_id": ctx["chat_id"],
                "listing_url": choice.get("url"), "listing_offer_id": choice.get("offer_id"),
                "created_by": config.SENDER}})
            if not s.get("terms_sent_at") and not any(a["type"] == "send_terms" for a in actions):
                actions.append({"type": "send_terms"})
            say(f"Thank you{hi}! Your invoice for {summary} is on its way, with our Terms & Conditions. "
                "Payment confirms your booking.", "say the invoice and terms are coming; payment confirms",
                "invoice")
            return done("invoice")
        if feats.get("invoice_request"):
            say("Your invoice is above 👆 Once paid, please send the proof of payment here and we'll confirm "
                "your booking.", "point to the invoice and ask for proof of payment", "invoice")
            return done("invoice")
        if feats.get("question"):
            actions.append({"type": "maybe_handoff", "reason": "question Davis has no facts for"})
            say("Good question! Let me confirm that with our team; they'll reply here shortly.",
                "answer the guest's question from the facts, or say the team will confirm", "open_question")
            return done("invoice")
        say(f"Noted, thank you{hi}! Once paid, please send the proof of payment here and we'll confirm your "
            "booking.", "acknowledge; ask for proof of payment once paid", "invoice")
        return done("invoice")

    # ── qualification and presenting houses ──────────────────────────────────
    changed = any(old.get(k) != slots.get(k) for k in ("city", "check_in", "check_out", "guests", "budget_usd",
                                                         "bedrooms"))
    city = _city(slots)
    if not slots.get("city") and not (slots.get("check_in") and slots.get("check_out")):
        greet = ""
        if stage == "new" or feats.get("greeting"):
            greet = "" if feats.get("bot_question") else f"Hello{hi}! I'm Davis from {config.BRAND} 🏡 "
        if stage == "present" and offers and not (feats.get("availability_query") or feats.get("greeting")):
            return _open_or_nudge(ctx, feats, slots, offers, say, actions, done, prefix)
        say(greet + "Which city would you like to stay in, and for which dates?",
            "greet and ask which city and dates", "qualify")
        return done("qualify")
    if not slots.get("city"):
        say("Which city would you like to stay in? We have homes in Harare, Bulawayo, Victoria Falls and more.",
            "ask which city", "qualify")
        return done("qualify")
    if not (slots.get("check_in") and slots.get("check_out")):
        if slots.get("check_in"):
            from .catalogue import _short
            q = f"Lovely! How many nights from {_short(slots['check_in'])}"
        else:
            q = f"Lovely, {city}! Which dates are you looking at"
        q += "?" if slots.get("guests") else ", and for how many guests?"
        say(q, "ask the missing dates (and party size)", "qualify")
        return done("qualify")

    if stage == "searching" and not changed and not feats.get("more_options"):
        say(f"Still checking live prices in {city} for {_when(slots['check_in'], slots['check_out'])}. "
            "I'll send the options the moment they're in.", "say we are still checking", "searching")
        return done("searching")
    if stage in ("new", "qualify") or changed or feats.get("more_options") or not offers:
        return _present(ctx, slots, cat, actions, say, done, out, resumed=False)
    return _open_or_nudge(ctx, feats, slots, offers, say, actions, done, prefix)


def _price_line(o: dict) -> str:
    if not o.get("usd_per_night"):
        return ""
    line = f"{_money(o['usd_per_night'])} per night"
    if o.get("usd_total") and o.get("nights"):
        line += f", {_money(o['usd_total'])} for {o['nights']} night{'s' if o['nights'] != 1 else ''}"
    return line


def _present(ctx, slots, cat, actions, say, done, out, resumed: bool) -> dict:
    city, when = _city(slots), _when(slots.get("check_in"), slots.get("check_out"))
    choice = out.get("choice")
    if resumed and choice and not choice.get("usd_per_night"):
        fresh = cat.resolve_choice({"option_ref": choice.get("ref_no")}, [], slots, "searching")
        if fresh and fresh.get("usd_per_night"):
            out["choice"] = fresh
            say(f"Good news: {fresh['ref']} is free for {when} at {_money(fresh.get('usd_total') or fresh['usd_per_night'])}"
                f"{' total' if fresh.get('usd_total') else ' per night'}. Please send your full name and ID number "
                "for the invoice.", "confirm price and ask name and ID", "collect_details")
            return done("details")
    options, status = cat.find_options(slots, True)
    if status == "ok":
        actions.append({"type": "send_options", "options": options})
        party = f" for {slots['guests']} guest{'s' if slots['guests'] != 1 else ''}" if slots.get("guests") else ""
        k = len(options)
        say(f"Here {'are' if k > 1 else 'is'} {k} home{'s' if k > 1 else ''} in {city} for {when}{party} 🏡 "
            "Reply with the KCER number you like and I'll prepare your invoice.",
            "introduce the houses being sent; ask which KCER they like", "present")
        return done("present")
    if status in ("none_dated", "no_listings") and not resumed:
        actions.append({"type": "request_refresh", "city": slots["city"], "check_in": slots["check_in"],
                        "check_out": slots["check_out"]})
        say(f"Checking live availability in {city} for {when} now. I'll send you the best options in a few "
            "minutes.", "say live availability is being checked", "searching")
        return done("searching")
    if status == "all_sent":
        say(f"Those are all the homes I have free in {city} for {when} right now. Would other dates or a nearby "
            "area work?", "say all options were sent; offer other dates or areas", "present")
        return done("present")
    say(f"Sorry, nothing is free in {city} for {when}. Would other dates or a nearby area work for you?",
        "say nothing is free; offer other dates or areas", "qualify")
    return done("qualify")


def _open_or_nudge(ctx, feats, slots, offers, say, actions, done, prefix) -> dict:
    latest = [o for o in offers if o.get("sent_at") == offers[0].get("sent_at")]
    latest.sort(key=lambda o: o.get("option_no") or 0)
    if feats.get("price_query") and latest:
        parts = [f"{o['ref']}: {_money(o['usd_per_night'])}/night" for o in latest[:3] if o.get("usd_per_night")]
        if parts:
            say("; ".join(parts) + ". Which one shall I reserve for you?", "list the prices; ask which to reserve",
                "price")
            return done("present")
    if feats.get("location_query") and latest:
        parts = [f"{o['ref']} is in {o['place']}" for o in latest[:3] if o.get("place")]
        if parts:
            say("; ".join(parts) + ". The exact address is shared once you're booked.",
                "say where the houses are", "location")
            return done("present")
    if feats.get("question") or not feats.get("ack_only"):
        actions.append({"type": "maybe_handoff", "reason": "question Davis has no facts for"})
        say("Good question! Let me confirm with our team and come back to you shortly.",
            "answer the guest's question from the facts, or say the team will confirm", "open_question")
        return done("present" if offers else "qualify")
    say("Which home would you like? Reply with its KCER number and I'll prepare your invoice.",
        "ask which house they want", "present")
    return done("present")


def _facts(slots: dict, choice: Optional[dict], offers: list[dict], actions: list[dict]) -> dict:
    sending = next((a["options"] for a in actions if a["type"] == "send_options"), [])
    return {"slots": slots, "choice": choice, "offered_before": offers[:6],
            "sending_now": [{k: o.get(k) for k in ("ref", "place", "capacity", "check_in", "check_out", "nights",
                                                     "usd_per_night", "usd_total", "rating", "note")
                             if o.get(k) is not None} for o in sending]}


def allowed_amounts(facts: dict) -> set[float]:
    vals: set[float] = set()
    pool = list(facts.get("offered_before") or []) + list(facts.get("sending_now") or [])
    if facts.get("choice"):
        pool.append(facts["choice"])
    for o in pool:
        for k in ("usd_per_night", "usd_total"):
            if o.get(k):
                vals.add(round(float(o[k]), 2))
    if (facts.get("slots") or {}).get("budget_usd"):
        vals.add(round(float(facts["slots"]["budget_usd"]), 2))
    return vals


_AMOUNT = re.compile(r"(?:usd|us\$|\$)\s*([\d,]+(?:\.\d{1,2})?)|([\d,]+(?:\.\d{1,2})?)\s*(?:usd|dollars)", re.I)
_REF = re.compile(r"\bKCER\s*(\d+)\b", re.I)


def check_reply(text: str, facts: dict) -> Optional[str]:
    """Why a model-written reply can't be sent (None = fine). Guards against
    the classic chatbot failures: invented prices, invented houses, links,
    naming the platform, too long."""
    if not text or not text.strip():
        return "empty"
    if nlu.word_count(text) > config.MAX_WORDS:
        return "too_long"
    if re.search(r"https?://|www\.|airbnb", text, re.I):
        return "link_or_platform"
    ok = allowed_amounts(facts)
    for m in _AMOUNT.finditer(text):
        raw = (m.group(1) or m.group(2) or "").replace(",", "")
        try:
            v = round(float(raw), 2)
        except ValueError:
            continue
        if not any(abs(v - a) < 0.01 for a in ok):
            return f"price_not_in_facts:{raw}"
    refs = {str(o.get("ref_no")) for o in (facts.get("offered_before") or [])}
    refs |= {re.sub(r"\D", "", o.get("ref") or "") for o in (facts.get("sending_now") or [])}
    if facts.get("choice"):
        refs.add(str(facts["choice"].get("ref_no")))
    for m in _REF.finditer(text):
        if m.group(1) not in refs:
            return f"unknown_house:KCER {m.group(1)}"
    return None
