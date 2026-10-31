"""davis/policy.py — what Davis does next: a frame-based booking dialogue.

The frame (city, dates, guests, budget, chosen house, name, ID) fills from
the guest's messages; the stage follows from what is filled:

    new → qualify → present → details → invoice → payment
                  ↘ searching (live homes requested) ↗

Every turn produces a plan: the actions Davis's "hands" perform (send a
reply, send houses with photos, send the Terms PDF, create the invoice, ask
the Airbnb scraper for live homes for the guest's exact dates) and the key
points of the reply. There is no hand-over: complaints, payments, open
questions and late prices are all answered by Davis itself.
The model, when one is configured, rewrites only the reply text; the actions
are decided here, with code, so a booking never depends on how a model felt
about it. Replies follow the guest-reception basics that close deals: answer
fast, one clear question per message (Grice's maxim of quantity), restate
what was agreed, always end with the next step.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

from . import config, nlu, persona
from .catalogue import capacity_guests as catalogue_guests, fee_label
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


def turn_features(texts: list[str], today, places: dict, has_image: bool = False,
                  context: Optional[list[str]] = None) -> dict:
    """Features of the guest's whole turn (several quick messages read as
    one): booleans OR-ed, slots newest-wins, plus ack_only. context = the
    recent conversation (guest and staff), for the month a bare "21 to 22"
    refers to."""
    agg: dict = {"texts": texts, "has_image": has_image}
    acks = []
    month = nlu.last_month(list(context or []) + list(texts))
    for t in texts:
        f = nlu.extract(t, today, places, month)
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
            hit = None if said else (cat.skill_answer(" ".join(feats.get("texts") or []))
                                     if hasattr(cat, "skill_answer") else None)
            if hit:
                # The team answered this kind of question before (a skill): reuse it.
                actions[:] = [a for a in actions if a["type"] != "reply"]
                actions.append({"type": "skill_used", "id": hit["id"]})
                reply(prefix + hit["answer"].strip() + " " + nxt(stage_),
                      "answer with the team's earlier answer", "skill")
                out["skill"] = {k: hit.get(k) for k in ("id", "question", "score")}
            if said:
                # A team note answers it: quote the note (exact).
                actions[:] = [a for a in actions if a["type"] != "reply"]
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
    # Davis never goes quiet and never hands over: a complaint, a request for
    # a person and a payment proof are all handled here. Only "Take over" on
    # the dashboard mutes Davis for a chat.
    def nxt(stage_: Optional[str] = None) -> str:
        return next_step(stage_ or stage, slots, out.get("choice") or s.get("choice"), offers)

    if trigger == "followup":
        return _followup(stage, slots, s.get("choice"), offers, hi, reply, done)
    if feats.get("complaint"):
        reply(L("complaint", hi=hi), "apologise, own the problem and ask exactly what went wrong", "complaint")
        return done(stage if stage != "new" else "qualify")
    if feats.get("human_request"):
        reply(L("person", next=nxt()), "say Davis handles everything here, then the next step", "person")
        return done(stage if stage != "new" else "qualify")
    if feats.get("payment_claim") or (feats.get("has_image") and stage in ("invoice", "payment")):
        reply(L("payment", hi=hi), "thank the guest for the proof; confirmation follows once it reflects", "payment")
        return done("payment")
    if feats.get("long_term") and not s.get("choice") and stage in ("new", "qualify", "present", "searching") \
            and not feats.get("explicit_choice"):
        # A monthly room or long lease: not what we do. Said kindly, once; no search.
        reply(L("long_term"), "explain kindly that we only do short stays of a few nights; no houses", "long_term")
        return done("qualify")

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
    turn_text = " ".join(feats.get("texts") or [])
    for answer in persona.faq_answers(turn_text):          # company, payment deadline, deposit, fee, website
        if answer not in prefix:
            prefix += answer + " "
        faq = True
    # Small talk ("Hi", "How are yu") is answered warmly, then the next step;
    # it is never treated as a question for the team.
    social = bool(feats.get("texts")) and all(
        len(t.split()) <= 5 and (persona.is_small_talk(t) or nlu.extract(t).get("greeting")) for t in feats["texts"])
    if any(persona.is_small_talk(t) for t in feats.get("texts") or []):
        prefix = L("well", hi=hi) + prefix
    elif feats.get("greeting") and stage not in ("new", "qualify"):
        prefix = L("hello", hi=hi) + prefix
    if social:
        faq = True

    def say(text: str, goal: str, intent: str) -> None:
        reply(prefix + text, goal, intent)

    # ── live homes arrived (or didn't) for a search Davis promised ───────────
    if trigger in ("refresh_done", "refresh_timeout") and slots.get("city") and slots.get("check_in") \
            and slots.get("check_out"):
        return _present(ctx, slots, cat, actions, say, done, out, resumed=True,
                        timed_out=trigger == "refresh_timeout", tries=s.get("refresh_tries") or 0)

    # ── photos of a house ("pics", "can I see KCER 105?") ────────────────────
    photos = _photo_houses(feats, offers, slots, stage, s, cat) if trigger == "message" else []
    if photos:
        actions.append({"type": "send_options", "options": photos, "max_images": config.IMAGES_PER_PHOTO_REQUEST,
                        "photos": True})
        if not feats.get("explicit_choice"):
            refs = " and ".join(h["ref"] for h in photos)
            say(L("photos", refs=refs, next=nxt("present" if stage in ("new", "qualify") else stage)),
                "send the photos they asked for, answer their other questions, then the next step", "photos")
            return done(stage if stage not in ("new", "qualify") else "present")

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
        say(L("no_detail", next=nxt("present")), f"answer the guest's question about {choice['ref']} from its note "
            "in FACTS, or say that detail isn't in the listing", "open_question")
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
                  f"{_money(choice.get('usd_due') or choice.get('usd_per_night'))}" \
                  f"{' ' + fee_label() if choice.get('usd_due') else ' per night'}"
        name, ident = slots.get("full_name"), slots.get("id_number")
        gave_details = feats.get("id_number") or feats.get("name_like") or feats.get("full_name") \
            or feats.get("long_number")
        if (not name or not ident) and feats.get("question") and not gave_details and not faq and not any(
                feats.get(k) for k in ("invoice_request", "price_query", "location_query")):
            say(L("no_detail", next=L("next_details")), "answer the guest's question from FACTS, or say that "
                "detail isn't in the listing", "open_question")
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
                say(L("no_detail", next="").strip(), "answer from FACTS, or say that detail isn't in the listing",
                    "open_question")
            else:
                say(L("payment_wait", hi=hi), "reassure: payment being confirmed, details follow", "payment")
            return done("payment")
        if feats.get("invoice_request"):
            say(L("invoice_above"), "point to the invoice and ask for proof of payment", "invoice")
            return done("invoice")
        if feats.get("question") and not faq:
            say(L("no_detail", next=L("next_pay")),
                "answer the guest's question from the facts, or say that detail isn't in the listing", "open_question")
            return done("invoice")
        say(L("pay_short") if faq else L("after_invoice", hi=hi), "acknowledge; ask for proof of payment once paid",
            "invoice")
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
    if o.get("usd_due") and o.get("nights"):
        line += f", {_money(o['usd_due'])} for {o['nights']} night{'s' if o['nights'] != 1 else ''} {fee_label()}"
    return line


def _photo_houses(feats: dict, offers: list[dict], slots: dict, stage: str, s: dict, cat) -> list[dict]:
    """The houses whose photos the guest asked for: the KCERs they named,
    else the house being discussed (chosen, or the only one just sent),
    else the last batch sent (up to 3)."""
    refs = list(feats.get("photos_for") or [])
    if not refs and feats.get("photos_request"):
        current = s.get("choice")
        latest = [o for o in offers if offers and o.get("sent_at") == offers[0].get("sent_at")]
        if feats.get("option_ref"):
            refs = [feats["option_ref"]]
        elif current and current.get("ref_no"):
            refs = [current["ref_no"]]
        else:
            refs = [o["ref_no"] for o in latest[:3] if o.get("ref_no")]
    out = []
    for r in refs[:3]:
        h = cat.resolve_choice({"option_ref": r}, offers, slots, stage)
        if h and h.get("url") and all(h["url"] != o["url"] for o in out):
            out.append(h)
    return out


def _present(ctx, slots, cat, actions, say, done, out, resumed: bool, timed_out: bool = False,
             tries: int = 0) -> dict:
    city, when = _city(slots), _when(slots.get("check_in"), slots.get("check_out"))
    choice = out.get("choice")
    if resumed and choice and not choice.get("usd_per_night"):
        fresh = cat.resolve_choice({"option_ref": choice.get("ref_no")}, [], slots, "searching")
        if fresh and fresh.get("usd_per_night"):
            out["choice"] = fresh
            say(L("good_news", ref=fresh["ref"], when=when, money=_money(fresh.get("usd_due") or fresh["usd_per_night"]),
                  total_word=" " + fee_label() if fresh.get("usd_due") else " per night"),
                "confirm price and ask name and ID", "collect_details")
            return done("details")
    options, status = cat.find_options(slots, True)
    if status == "too_small" and options:
        actions.append({"type": "send_options", "options": options})
        most = max((catalogue_guests(o.get("capacity")) or 0) for o in options)
        say(L("too_small", city=city, when=when, guests=slots.get("guests"), most=most or "fewer"),
            "say no single home sleeps the whole party; send the biggest; offer two homes", "present")
        return done("present")
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
    if status in ("none_dated", "no_listings") and timed_out:
        if tries < config.REFRESH_TRIES:
            # The scraper may have been busy or restarting: ask once more,
            # quietly (the guest was already told homes are on the way).
            actions.append({"type": "request_refresh", "city": slots["city"], "check_in": slots["check_in"],
                            "check_out": slots["check_out"], "retry": True})
            return done("searching")
        say(L("prices_none", city=city, when=when), "say no live prices came; offer other dates or areas", "qualify")
        return done("qualify")
    if status == "all_sent":
        say(L("all_sent", city=city, when=when), "say all options were sent; offer other dates or areas", "present")
        return done("present")
    say(L("none_free", city=city, when=when),
        "say nothing is free; offer other dates or areas", "qualify")
    return done("qualify")


def _followup(stage, slots, choice, offers, hi, reply, done) -> dict:
    """The one nudge for a guest who went quiet mid-booking (engine's sweep
    decides when). Davis keeps the last word, gently."""
    ref = (choice or {}).get("ref") or ""
    if stage == "invoice" and ref:
        reply(L("followup_invoice", hi=hi, ref=ref), "check in on the unpaid invoice; ask for proof once paid", "followup")
    elif stage in ("details", "searching") and ref:
        reply(L("followup_details", hi=hi, ref=ref), "ask whether to go ahead; ask name and ID", "followup")
    elif offers or stage == "present":
        reply(L("followup_present", hi=hi), "ask whether a home caught their eye; offer to reserve it", "followup")
    else:
        reply(L("followup_qualify", hi=hi), "ask if they still need a stay; ask city and dates", "followup")
    return done(stage)


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
    if not feats.get("question"):
        # A statement, not a question ("Yes confirm please"): nothing for the team, just the next step.
        agree = feats.get("affirm") or re.search(r"\b(?:confirm|yes|yeah|sure|ok(?:ay)?|cool|fine)\b",
                                                 " ".join(feats.get("texts") or []).lower())
        say((L("great") if agree else "") + (L("next_pick") if offers else L("next_city_dates")),
            "ask which house they want", "present")
        return done("present" if offers else "qualify")
    if feats.get("question"):
        say(L("no_detail", next=L("next_pick") if offers else L("next_city_dates")),
            "answer the guest's question from the facts, or say that detail isn't in the listing", "open_question")
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
                                                     "usd_per_night", "usd_total", "usd_due", "rating", "note")
                             if o.get(k) is not None} for o in sending]}


def allowed_amounts(facts: dict) -> set[float]:
    vals: set[float] = set()
    pool = list(facts.get("offered_before") or []) + list(facts.get("sending_now") or [])
    pool += [facts[k] for k in ("choice", "asked_about") if facts.get(k)]
    for o in pool:
        for k in ("usd_per_night", "usd_total", "usd_due"):
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
        elif len(v) > 1 and v.startswith("0") and len(v) <= 2:
            v = v.lstrip("0") or "0"          # "08" in 2026-11-08 is the 8 of "8 Nov"
        out.add(v)
    return out


def check_reply(text: str, facts: dict, draft: Optional[str] = None, keep_draft: bool = False,
                keep_numbers: bool = False, max_words: Optional[int] = None) -> Optional[str]:
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
    if keep_numbers and draft:
        missing = sorted(_numbers(draft) - _numbers(text))
        if missing:
            return "dropped:" + ",".join(missing[:3])           # e.g. the EcoCash number or the total
    if nlu.word_count(text) > (max_words or config.MAX_WORDS):
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
