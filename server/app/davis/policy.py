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
from .persona import line as L

STAGES = ("new", "qualify", "searching", "present", "details", "invoice", "payment", "opted_out")


def _city(slots: dict) -> str:
    return (slots.get("city") or "").title()


def _first_name(slots: dict, chat_name: str) -> str:
    if slots.get("full_name"):
        return slots["full_name"].split()[0]
    name = (chat_name or "").strip()
    if name.lower() in ("guest", "unknown", "customer", "me", "you"):
        return ""
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
    if stage in ("new", "qualify") and offers:
        stage = "present"                 # houses were already sent (by staff from the app, or by Davis)
    first = _first_name(slots, ctx.get("chat_name", ""))
    hi = f" {first}" if first else ""
    actions: list[dict] = []
    out = {"stage": stage, "slots": slots, "choice": s.get("choice"), "actions": actions,
           "intent": "", "goal": "", "facts": {}}

    def reply(text: str, goal: str, intent: str) -> None:
        actions.append({"type": "reply", "text": nlu.clip_words(text, config.MAX_WORDS)})
        out["goal"], out["intent"] = goal, intent

    def done(stage_: str) -> dict:
        out["slots"] = slots          # slots may have been rebound (merge_slots returns a new dict)
        if out.get("intent") == "open_question":
            facts_ = _facts(slots, out.get("choice"), offers, actions)
            terms = note_overlap(" ".join(feats.get("texts") or []), facts_, out.get("asked_about"))
            out["note_terms"] = sorted(terms)
            houses = [o for o in [out.get("asked_about"), out.get("choice")] + list(facts_.get("sending_now") or [])
                      + list(offers) if o and o.get("note") and any(t in o["note"].lower() for t in terms)]
            seen, said = set(), []
            for o in houses:
                if o.get("ref") not in seen:
                    seen.add(o.get("ref"))
                    said.append(f"{o['ref']} has {o['note'].strip().rstrip('.')}")
            if said:
                # A team note answers it: quote the note (exact), no handoff needed.
                actions[:] = [a for a in actions if a["type"] not in ("maybe_alert", "reply")]
                key = "note_answer_after" if stage_ in ("invoice", "payment") else "note_answer"
                reply(prefix + L(key, notes="; ".join(said[:2])),
                      "answer the guest's question from the team's note in FACTS", "open_question")
        order = {"reply": 0, "send_options": 1, "create_invoice": 2, "send_terms": 3}
        actions.sort(key=lambda a: order.get(a["type"], 9))         # the reply leads
        out["stage"] = stage_
        out["facts"] = _facts(slots, out.get("choice"), offers, actions)
        return out

    # ── safety first: the guest's own wishes about the conversation ──────────
    if feats.get("opt_in") and s.get("opted_out"):
        actions.append({"type": "opt_in"})
        reply(L("welcome_back", hi=hi),
              "welcome the guest back and ask city and dates", "opt_in")
        return done("qualify")
    if feats.get("opt_out"):
        actions.append({"type": "opt_out"})
        reply(L("opt_out"),
              "confirm we will stop messaging", "opt_out")
        return done("opted_out")
    # Davis never goes quiet on a guest: a complaint, a request for a person or
    # a payment to confirm ALERTS the team (dashboard) and Davis keeps the
    # conversation going. Only "Take over" on the dashboard mutes Davis.
    def nxt(stage_: Optional[str] = None) -> str:
        return next_step(stage_ or stage, slots, out.get("choice") or s.get("choice"), offers)

    if feats.get("complaint") or feats.get("human_request"):
        reason = "complaint" if feats.get("complaint") else "asked for a person"
        actions.append({"type": "alert_staff", "reason": reason})
        reply(L("alert_complaint") if feats.get("complaint") else L("alert_person", next=nxt()),
              "tell the guest the team is alerted and keep helping", "alert")
        return done(stage if stage != "new" else "qualify")
    if feats.get("payment_claim") or (feats.get("has_image") and stage in ("invoice", "payment")):
        actions.append({"type": "alert_staff", "reason": "payment proof to verify"})
        reply(L("payment", hi=hi), "thank the guest; staff will confirm the payment", "payment")
        return done("payment")

    prefix = ""
    if feats.get("bot_question"):
        hello = L("hello", hi=hi) if stage == "new" or feats.get("greeting") else ""
        prefix = hello + L("bot")
    if feats.get("invoice_request") and stage not in ("invoice", "payment"):
        prefix += L("invoice_hint")              # "how do I pay?" before the invoice: EcoCash details
    if feats.get("terms_request") and not s.get("terms_sent_at"):
        actions.append({"type": "send_terms"})
        prefix += L("terms_attached")
    # Standing answers (house rules of the business), then the normal next step.
    faq = False
    if feats.get("viewing_request"):
        prefix += L("no_viewing", next="").strip() + " "
        faq = True
    if feats.get("checkin_query"):
        prefix += L("checkin_times", next="").strip() + " "
        faq = True

    def say(text: str, goal: str, intent: str) -> None:
        reply(prefix + text, goal, intent)

    # ── live prices arrived for a search Davis promised ──────────────────────
    if trigger == "refresh_done" and slots.get("city") and slots.get("check_in") and slots.get("check_out"):
        return _present(ctx, slots, cat, actions, say, done, out, resumed=True)

    # ── a house picked (or asked about) ──────────────────────────────────────
    choice = cat.resolve_choice(feats, offers, slots, stage)
    tentative = bool(choice) and feats.get("question") and not feats.get("explicit_choice") \
        and not feats.get("affirm")
    about_house = bool(choice) and not faq and feats.get("question") and not feats.get("explicit_choice") \
        and not feats.get("affirm") and not any(feats.get(k) for k in ("price_query", "availability_query",
                                                                        "location_query", "invoice_request")) \
        and (feats.get("words") or 0) > 4
    if about_house:                                           # "does KCER 102 have wifi?"
        out["choice"] = choice if not s.get("choice") or stage == "present" else s["choice"]
        facts_house = dict(choice)
        actions.append({"type": "maybe_alert", "reason": "question about " + choice["ref"]})
        say(L("team_confirm_soon", next=nxt("present")), f"answer the guest's question about {choice['ref']} from its note in FACTS, "
            "or say the team will confirm", "open_question")
        out["asked_about"] = facts_house
        res = done(stage if stage not in ("new", "qualify") else "present")
        res["facts"]["asked_about"] = facts_house
        return res
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
            say(L("price_tentative", ref=choice["ref"], place=choice.get("place") or _city(slots),
                  price=", " + price if price else ""), "answer about the house and offer to reserve it", "price")
            return done("present")
        stage = "details"
    elif s.get("choice") and stage in ("details", "invoice", "searching", "payment"):
        choice = s["choice"]
        new_dates = (slots.get("check_in"), slots.get("check_out")) != (choice.get("check_in"), choice.get("check_out"))
        if new_dates and slots.get("check_in") and slots.get("check_out"):
            choice = cat.resolve_choice({"option_ref": choice.get("ref_no")}, [], slots, stage) or choice
        out["choice"] = choice

    # ── collecting the invoice details ───────────────────────────────────────
    if choice and stage in ("details", "invoice", "searching", "payment"):
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
                say(L("confirm_live_price", ref=choice["ref"], when=_when(slots["check_in"], slots["check_out"])),
                    "say live price is being checked", "searching")
                return done("searching")
            say(L("ask_dates_for_choice", ref=choice["ref"]),
                "ask the dates for the chosen house", "qualify")
            return done("details")
        summary = f"{choice['ref']}, {_when(choice.get('check_in'), choice.get('check_out'))}, " \
                  f"{_money(choice.get('usd_total') or choice.get('usd_per_night'))}" \
                  f"{' total' if choice.get('usd_total') else ' per night'}"
        name, ident = slots.get("full_name"), slots.get("id_number")
        gave_details = feats.get("id_number") or feats.get("name_like") or feats.get("full_name") \
            or feats.get("long_number")
        if (not name or not ident) and feats.get("question") and not gave_details and not faq and not any(
                feats.get(k) for k in ("invoice_request", "price_query", "location_query")):
            actions.append({"type": "maybe_alert", "reason": "question Davis has no facts for"})
            say(L("team_confirm_soon", next=L("next_details")), "answer the guest's question from FACTS, or say the team will confirm",
                "open_question")
            return done(stage)
        if (not name or not ident) and feats.get("price_query") and L("invoice_hint") not in prefix:
            prefix += L("invoice_hint")
        if not name and not ident:
            # After an answer (EcoCash, viewings, check-in) the short ask keeps the reply under 40 words.
            say(L("next_details") if prefix else L("ask_name_id", summary=summary),
                "confirm the choice and ask full name and ID number", "collect_details")
            return done("details")
        if not ident:
            say(L("ask_id", hi=hi),
                "ask the ID or passport number", "collect_details")
            return done("details")
        if not name:
            say(L("ask_name"),
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
            say(L("invoice_sent", hi=hi, summary=summary),
                "say the invoice and terms are coming; payment confirms", "invoice")
            return done("invoice")
        if stage == "payment":
            real_question = feats.get("question") and not feats.get("greeting") and (feats.get("words") or 0) > 2
            if real_question and not faq and not feats.get("invoice_request"):
                actions.append({"type": "maybe_alert", "reason": "question after payment"})
                say(L("team_confirm", next=""), "answer from FACTS, or say the team will confirm", "open_question")
            else:
                say(L("payment_wait", hi=hi), "reassure: payment being confirmed, details follow", "payment")
            return done("payment")
        if feats.get("invoice_request"):
            say(L("invoice_above"), "point to the invoice and ask for proof of payment", "invoice")
            return done("invoice")
        if feats.get("question") and not faq:
            actions.append({"type": "maybe_alert", "reason": "question Davis has no facts for"})
            say(L("team_confirm", next=L("next_pay")),
                "answer the guest's question from the facts, or say the team will confirm", "open_question")
            return done("invoice")
        say(L("after_invoice", hi=hi), "acknowledge; ask for proof of payment once paid", "invoice")
        return done("invoice")

    # ── qualification and presenting houses ──────────────────────────────────
    changed = any(old.get(k) != slots.get(k) for k in ("city", "check_in", "check_out", "guests", "budget_usd",
                                                         "bedrooms"))
    city = _city(slots)
    if not slots.get("city") and not (slots.get("check_in") and slots.get("check_out")):
        greet = ""
        if stage == "new" or feats.get("greeting"):
            greet = "" if feats.get("bot_question") else (L("hello", hi=hi) if prefix else L("greet", hi=hi))
        if stage == "present" and offers and (faq or feats.get("invoice_request") or feats.get("question")
                                              or not (feats.get("availability_query") or feats.get("greeting"))):
            return _open_or_nudge(ctx, feats, slots, offers, say, actions, done, prefix, faq)
        reply(greet + prefix + L("ask_city_dates"),          # the greeting always comes first
              "greet and ask which city and dates", "qualify")
        return done("qualify")
    if not slots.get("city"):
        say(L("ask_city"),
            "ask which city", "qualify")
        return done("qualify")
    if not (slots.get("check_in") and slots.get("check_out")):
        if slots.get("check_in"):
            from .catalogue import _short
            q = L("ask_nights", start=_short(slots["check_in"]))
        else:
            q = L("ask_dates", city=city)
        q += "?" if slots.get("guests") else L("ask_guests_tail")
        say(q, "ask the missing dates (and party size)", "qualify")
        return done("qualify")

    if stage == "searching" and not changed and not feats.get("more_options"):
        say(L("still_checking", city=city, when=_when(slots["check_in"], slots["check_out"])),
            "say we are still checking", "searching")
        return done("searching")
    if stage in ("new", "qualify") or changed or feats.get("more_options") or not offers:
        return _present(ctx, slots, cat, actions, say, done, out, resumed=False)
    return _open_or_nudge(ctx, feats, slots, offers, say, actions, done, prefix, faq)


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
            say(L("good_news", ref=fresh["ref"], when=when, money=_money(fresh.get("usd_total") or fresh["usd_per_night"]),
                  total_word=" total" if fresh.get("usd_total") else " per night"),
                "confirm price and ask name and ID", "collect_details")
            return done("details")
    options, status = cat.find_options(slots, True)
    if status == "ok":
        actions.append({"type": "send_options", "options": options})
        party = f" for {slots['guests']} guest{'s' if slots['guests'] != 1 else ''}" if slots.get("guests") else ""
        k = len(options)
        say(L("present", are="are" if k > 1 else "is", k=k, homes="homes" if k > 1 else "home", city=city,
              when=when, party=party),
            "introduce the houses being sent; ask which KCER they like", "present")
        return done("present")
    if status in ("none_dated", "no_listings") and not resumed:
        actions.append({"type": "request_refresh", "city": slots["city"], "check_in": slots["check_in"],
                        "check_out": slots["check_out"]})
        say(L("checking", city=city, when=when), "say live availability is being checked", "searching")
        return done("searching")
    if status == "all_sent":
        say(L("all_sent", city=city, when=when), "say all options were sent; offer other dates or areas", "present")
        return done("present")
    say(L("none_free", city=city, when=when),
        "say nothing is free; offer other dates or areas", "qualify")
    return done("qualify")


def next_step(stage: str, slots: dict, choice: Optional[dict], offers: list) -> str:
    """The question that moves this booking on: every reply ends with one."""
    if stage == "payment":
        return ""
    if stage == "invoice":
        return L("next_pay")
    if choice and stage in ("details", "searching"):
        return L("next_details")
    if offers or stage == "present":
        return L("next_pick")
    return L("next_city_dates")


def _open_or_nudge(ctx, feats, slots, offers, say, actions, done, prefix, faq=False) -> dict:
    latest = [o for o in offers if o.get("sent_at") == offers[0].get("sent_at")]
    latest.sort(key=lambda o: o.get("option_no") or 0)
    if feats.get("price_query") and latest:
        parts = [f"{o['ref']}: {_money(o['usd_per_night'])}/night" for o in latest[:3] if o.get("usd_per_night")]
        if parts:
            say("; ".join(parts) + L("price_list_tail"), "list the prices; ask which to reserve",
                "price")
            return done("present")
    if feats.get("location_query") and latest:
        parts = [f"{o['ref']} is in {o['place']}" for o in latest[:3] if o.get("place")]
        if parts:
            say("; ".join(parts) + L("location_tail"),
                "say where the houses are", "location")
            return done("present")
    if feats.get("invoice_request") or faq:      # the answer is already in prefix
        say(L("next_pick") if offers else L("next_city_dates"), "answer, then ask which house they want",
            "payment_info" if feats.get("invoice_request") else "faq")
        return done("present" if offers else "qualify")
    if feats.get("question") or not feats.get("ack_only"):
        actions.append({"type": "maybe_alert", "reason": "question Davis has no facts for"})
        say(L("team_confirm_soon", next=L("next_pick") if offers else L("next_city_dates")),
            "answer the guest's question from the facts, or say the team will confirm", "open_question")
        return done("present" if offers else "qualify")
    say(L("which_home"),
        "ask which house they want", "present")
    return done("present")


_STOP = set("""a an the is are was were be been do does did can could will would shall should may might
i we you he she it they me us my our your its their this that these those there here what which who whom
whose when where why how any some much many more most no not yes please pls hi hello hey ok okay thanks
thank kcer house home place stay booking book have has had with for of in on at to from by about near
get got want need like also just there's is there there are""".split())


def note_overlap(question: str, facts: dict, asked_about: Optional[dict] = None) -> set[str]:
    """Words of the guest's question that appear in a team note of the
    houses in play ("wifi" in "wifi, pool, solar backup"). The model may
    only answer an open question when this is not empty, and its answer
    must use one of these words: a small model otherwise invents facts."""
    pool = list(facts.get("offered_before") or []) + list(facts.get("sending_now") or [])
    pool += [o for o in (facts.get("choice"), asked_about) if o]
    notes = " ".join((o.get("note") or "") for o in pool).lower()
    if not notes:
        return set()
    note_words = {w.rstrip("s") for w in re.findall(r"[a-z][a-z\-]+", notes)}
    out = set()
    for w in re.findall(r"[a-z][a-z\-]+", (question or "").lower()):
        if len(w) > 2 and w not in _STOP and (w.rstrip("s") in note_words):
            out.add(w.rstrip("s"))
    return out


def _facts(slots: dict, choice: Optional[dict], offers: list[dict], actions: list[dict]) -> dict:
    sending = next((a["options"] for a in actions if a["type"] == "send_options"), [])
    return {"slots": slots, "choice": choice, "offered_before": offers[:6],
            "sending_now": [{k: o.get(k) for k in ("ref", "place", "capacity", "check_in", "check_out", "nights",
                                                     "usd_per_night", "usd_total", "rating", "note")
                             if o.get(k) is not None} for o in sending]}


def allowed_amounts(facts: dict) -> set[float]:
    vals: set[float] = set()
    pool = list(facts.get("offered_before") or []) + list(facts.get("sending_now") or [])
    pool += [facts[k] for k in ("choice", "asked_about") if facts.get(k)]
    for o in pool:
        for k in ("usd_per_night", "usd_total"):
            if o.get(k):
                vals.add(round(float(o[k]), 2))
    if (facts.get("slots") or {}).get("budget_usd"):
        vals.add(round(float(facts["slots"]["budget_usd"]), 2))
    return vals


_AMOUNT = re.compile(r"(?:usd|us\$|\$)\s*([\d,]+(?:\.\d{1,2})?)|([\d,]+(?:\.\d{1,2})?)\s*(?:usd|dollars)", re.I)
_REF = re.compile(r"\bKCER\s*(\d+)\b", re.I)


_GUEST_VOICE = re.compile(r"\b(?:hello|hi|hey|dear|thanks|thank you),?\s+davis\b|\bI(?:'d| would) like to "
                          r"(?:stay|book|rent)\b|\bI(?:'m| am) looking for\b|\bcan you (?:send|find) me\b", re.I)
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    out = set()
    for m in _NUM.finditer(text or ""):
        v = m.group(0).replace(",", "").rstrip(".")
        if "." in v:
            v = v.rstrip("0").rstrip(".")
        out.add(v)
    return out


def check_reply(text: str, facts: dict, draft: Optional[str] = None, keep_draft: bool = False) -> Optional[str]:
    """Why a model-written reply can't be sent (None = fine). Guards against
    the classic failures of a small model: invented prices, dates or houses,
    writing as the guest, dropping what the draft had to say, links,
    naming the platform, too long."""
    if not text or not text.strip():
        return "empty"
    if _GUEST_VOICE.search(text):
        return "wrote_as_guest"
    from .prompts import facts_text
    allowed = _numbers((draft or "") + " " + facts_text(facts))
    for n in _numbers(text):
        if n not in allowed:
            return f"number_not_in_facts:{n}"
    if keep_draft and draft:
        for ref in _REF.findall(draft):
            if not re.search(rf"\bKCER\s*{ref}\b", text, re.I):
                return f"dropped:KCER {ref}"
        for m in _AMOUNT.finditer(draft):
            raw = (m.group(1) or m.group(2) or "").replace(",", "")
            if raw and raw.rstrip("0").rstrip(".") not in _numbers(text):
                return f"dropped:USD {raw}"
        if draft.rstrip().endswith("?") and "?" not in text:
            return "dropped:question"
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
    for k in ("choice", "asked_about"):
        if facts.get(k):
            refs.add(str(facts[k].get("ref_no")))
    for m in _REF.finditer(text):
        if m.group(1) not in refs:
            return f"unknown_house:KCER {m.group(1)}"
    return None
