"""Karl: the redesign after reading the real October chats. The model reads
the whole conversation into the booking state (mind.py), code checks it and
keeps the actions; the cases below are taken from those chats."""
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from test_davis import CI, CO, GUEST, TODAY, _davis, _guest, _house, _human, _run, _setup, dc, davis_env  # noqa: F401


def _staff(db, text, minutes_ago=0.0, sender="You (WhatsApp)"):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="microseconds")
    db.execute("INSERT INTO messages (id, chat_id, sender, kind, text, created_at, direction, wa_status) "
               "VALUES (?,?,?,?,?,?, 'out', NULL)", (str(uuid.uuid4()), GUEST, sender, "text", text, ts))
    db.commit()


def _understanding(**kw):
    base = {"city": None, "dates_text": None, "check_in": None, "check_out": None, "guests": None,
            "stay_type": "unknown", "chosen_ref": None, "photos_for": [], "full_name": None, "id_number": None,
            "open_questions": []}
    base.update(kw)
    return base


def _fake_model(monkeypatch, understanding=None, reply=None, error=None):
    """Stand in for the local llama.cpp model: answers the STATE schema with
    `understanding` and the REPLY schema with `reply` (a string, or a
    function of the prompt). Records every call."""
    from app.davis import local_llm, mind
    calls = []

    def generate(msgs, schema, max_tokens=None):
        calls.append({"system": msgs[0]["content"], "user": msgs[-1]["content"], "schema": schema,
                      "max_tokens": max_tokens})
        if error:
            raise RuntimeError(error)
        if schema.get("required") == mind.STATE_SCHEMA["required"]:
            u = understanding(msgs[-1]["content"]) if callable(understanding) else understanding
            return u or _understanding(), {"model": "test.gguf"}
        text = reply(msgs[-1]["content"]) if callable(reply) else reply
        return {"reply": text or "Noted 👍"}, {"model": "test.gguf"}
    monkeypatch.setattr(local_llm.model, "generate", generate)
    return calls


def _points(prompt):
    """What the prompt says this message must get across (a well-behaved model keeps it)."""
    line = next(l for l in prompt.splitlines() if l.startswith("MUST GET ACROSS"))
    return line.split(": ", 1)[1]


def _karl(dc):
    assert dc.put("/api/davis/settings", json={"provider": "local", "llm_for": "ai"}).status_code == 200


# ─────────────────────────── understanding, from the chats ──────────────────

def _days_ahead(first: int, last: int):
    """Where code puts "from 23 to 25" with no month: this month if the 23rd
    is still ahead, else next month."""
    from app.davis.engine import Engine
    today = Engine._today()
    y, m = today.year, today.month
    if first < today.day:
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return date(y, m, first), date(y, m, last)


def test_dates_without_a_month_are_read_and_confirmed(dc, db, monkeypatch):
    """Rusape chat: "From 23 to 25 for two guests" was asked for again.
    Karl takes the dates (the model finds the words, code does the
    calendar), says it assumed the month, and checks live homes."""
    _setup(dc, db, houses=False)
    ci, co = _days_ahead(23, 25)
    calls = _fake_model(monkeypatch, _understanding(
        city="rusape", dates_text="From 23 to 25", check_in=None, check_out=None, guests=2, stay_type="short_stay"),
        reply=lambda p: f"Lovely, Rusape for 2 guests, {_human(ci)} to {_human(co)}: I'm checking live homes now. "
                        f"Is that the right month?")
    from app.davis import config
    monkeypatch.setattr(config, "KARL_UNDERSTAND", "always")
    monkeypatch.setattr(config, "KARL_REPLY", "always")
    _karl(dc)
    _guest(db, "Greetings", 10)
    _staff(db, "Hello! I'm Karl from KarlCon Elite Retreats 🏡 Which city would you like to stay in, and for which dates?",
           9, sender="Karl")
    _guest(db, "Rusape", 8)
    _staff(db, "Lovely, Rusape! Which dates are you looking at, and for how many guests?", 7, sender="Karl")
    _guest(db, "From 23 to 25 for two guests")
    r = _run(dc)
    assert "plan" in r, r
    assert r["plan"]["stage"] == "searching" and r["refresh_job_id"]
    job = dc.get(f"/api/houses/refresh/{r['refresh_job_id']}").json()
    assert (job["location"], job["check_in"], job["check_out"]) == ("rusape", ci.isoformat(), co.isoformat())
    understand, write = calls[0], calls[1]
    assert "From 23 to 25 for two guests" in understand["user"] and "GUEST: Rusape" in understand["user"]
    assert "dates was assumed" in write["user"]
    assert write["user"].index("NEWEST MESSAGES") > write["user"].index("CONVERSATION")   # the newest goes last
    # STATE and REPLY open identically: llama.cpp evaluates the conversation once
    head = understand["user"].split("=== TASK")[0]
    assert write["user"].startswith(head) and understand["system"] == write["system"]
    assert understand["user"].startswith("CONVERSATION")          # append-only: the conversation comes first
    assert understand["max_tokens"] and write["max_tokens"]
    assert r["plan"]["actions"][0]["by"] == "model" and "Rusape" in _davis(db)[-1]["text"]


def test_questions_are_answered_first_and_photos_sent(dc, db, monkeypatch):
    """Bulawayo chat: "May I kindly have pics / what amenities / where is it
    located" got a generic line. Karl sends the photos and the model gets
    the questions as the last thing it reads."""
    _setup(dc, db)
    _fake_model(monkeypatch, _understanding(city="harare", check_in=CI.isoformat(), check_out=CO.isoformat(),
                                              guests=4))
    _karl(dc)
    _guest(db, f"Harare {_human(CI)} to {_human(CO)} for 4")
    _run(dc)
    calls = _fake_model(monkeypatch, _understanding(
        city="harare", check_in=CI.isoformat(), check_out=CO.isoformat(), guests=4, photos_for=[102],
        open_questions=["send pictures of KCER 102", "what amenities does it have", "where is it located"]),
        reply="Here are more photos of KCER 102 📸 It sleeps 8 in Greendale; the listing doesn't list amenities. "
              "Shall I reserve it for you?")
    _guest(db, "May I kindly have pics of KCER 102")
    _guest(db, "And what amenities available")
    _guest(db, "Where is this located")
    r = _run(dc)
    sends = [a for a in r["plan"]["actions"] if a["type"] == "send_options"]
    assert sends and sends[0]["max_images"] == 8 and [o["ref"] for o in sends[0]["options"]] == ["KCER 102"]
    prompt = calls[-1]["user"]
    assert prompt.index("ANSWER FIRST") > prompt.index("CONVERSATION")
    assert "1. send pictures of KCER 102" in prompt and "3. where is it located" in prompt
    said = [m["text"] for m in _davis(db) if m["sender"] == "Karl"][-1]
    assert said.startswith("Here are more photos of KCER 102") and any(m["kind"] == "image" for m in _davis(db)[-6:])


def test_name_on_its_own_line_is_kept(dc, db, monkeypatch):
    """Bulawayo chat: "Blessing Nyasha Mpofu" was answered with "Which city"."""
    _setup(dc, db)
    _fake_model(monkeypatch, _understanding(city="harare", check_in=CI.isoformat(), check_out=CO.isoformat(),
                                              guests=4))
    _karl(dc)
    _guest(db, f"Harare {_human(CI)} to {_human(CO)} for 4")
    _run(dc)
    _fake_model(monkeypatch, _understanding(city="harare", check_in=CI.isoformat(), check_out=CO.isoformat(),
                                              guests=4, chosen_ref=102),
                 reply=_points)
    _guest(db, "KCER 102")
    r = _run(dc)
    assert "plan" in r, r
    assert r["plan"]["stage"] == "details"
    _fake_model(monkeypatch, _understanding(city="harare", check_in=CI.isoformat(), check_out=CO.isoformat(),
                                              guests=4, chosen_ref=102, full_name="Blessing Nyasha Mpofu"),
                 reply=lambda p: "Thank you Blessing! " + _points(p).replace("Thank you Blessing! ", ""))
    _guest(db, "Blessing Nyasha Mpofu")
    r = _run(dc)
    assert r["plan"]["slots"]["full_name"] == "Blessing Nyasha Mpofu" and r["plan"]["stage"] == "details"
    assert "ID" in _davis(db)[-1]["text"] and "city" not in _davis(db)[-1]["text"].lower()


def test_model_output_is_checked_before_use():
    from app.davis import mind
    today = date(2026, 10, 19)
    f = mind.to_features(_understanding(check_in="2026-10-01", check_out="2026-10-03", guests=500,
                                        full_name="R2D2 unit", id_number="ABCDEF", chosen_ref=-3), today, {}, [])
    assert not any(k in f for k in ("check_in", "check_out", "guests", "full_name", "id_number", "option_ref"))
    f = mind.to_features(_understanding(check_in="2026-11-21", check_out="2026-11-20"), today, {}, [])
    assert f["check_in"] == "2026-11-21" and "check_out" not in f            # no end before the start
    f = mind.to_features(_understanding(city="Avondale", check_in="2026-11-21", check_out="2026-11-22", guests=15,
                                        id_number="63-1234567 x 42", stay_type="long_term_rental"), today,
                         {"avondale": "harare"}, [])
    assert (f["city"], f["nights"], f["guests"], f["id_number"], f["long_term"]) == \
        ("harare", 1, 15, "63-1234567 X 42", True)


def test_a_model_failure_still_answers(dc, db, monkeypatch):
    _setup(dc, db)
    _fake_model(monkeypatch, error="model crashed")
    _karl(dc)
    _guest(db, f"Harare {_human(CI)} to {_human(CO)} for 4")
    r = _run(dc)                                   # the rules read the message; the plan's wording goes at once
    assert r["status"] == "sent" and r["plan"]["stage"] == "present"


def test_monthly_room_gets_a_kind_short_stay_answer(dc, db):
    """Marondera chat: "Room to rent from 80 to 90 per month" was sent holiday homes."""
    _setup(dc, db)
    _guest(db, "Room to rent from 80 to90 per month")
    r = _run(dc)
    assert r["plan"]["intent"] == "long_term" and not any(a["type"] in ("send_options", "request_refresh")
                                                          for a in r["plan"]["actions"])
    assert "short stays" in _davis(db)[-1]["text"]


def test_a_message_answered_by_staff_first_still_counts(dc, db):
    """Harare chat: "4november" came just before a staff reply, so no pass
    read it, and Karl asked for the dates again."""
    _setup(dc, db, houses=False)
    _guest(db, "Avondale Mountplesent borwdale", 5)
    _run(dc)
    _guest(db, "4november", 3)
    _staff(db, "okay noted", 2)
    _guest(db, "One guest")
    r = _run(dc)
    slots = r["plan"]["slots"]
    assert slots["city"] == "harare" and slots["check_in"].endswith("-11-04") and slots["guests"] == 1
    assert "Which dates" not in _davis(db)[-1]["text"] and "nights" in _davis(db)[-1]["text"]


def test_dates_on_house_cards_are_not_the_guests(dc, db):
    """Marondera chat: Karl "checked 19–21 Oct", dates the guest never gave:
    they came from the team's house cards."""
    _setup(dc, db, houses=False)
    _guest(db, "Looking for 1 room in town", 6)
    _staff(db, "🏠 KCER 111\n📍 Marondera\n👥 6 guests · 4 bedrooms\n📅 Free 18 Oct 2026 → 20 Oct 2026 (2 nights)\n"
               "💵 USD 133.50 per night · USD 267.00 total", 4)
    _guest(db, "MaronderA town")
    r = _run(dc)
    assert r["plan"]["slots"].get("city") == "marondera" and not r["plan"]["slots"].get("check_in")
    assert not any(a["type"] == "request_refresh" for a in r["plan"]["actions"])


def test_a_party_too_big_for_one_home_is_told_honestly(dc, db):
    """Bulawayo chat: 15 guests were offered homes for 6 and 8 as if they fit."""
    _setup(dc, db)
    _guest(db, f"Harare {_human(CI)} to {_human(CO)} for 15 guests")
    r = _run(dc)
    text = _davis(db)[0]["text"] if _davis(db) else ""
    sends = [a for a in r["plan"]["actions"] if a["type"] == "send_options"]
    assert sends and sends[0]["options"][0]["ref"] == "KCER 102"            # the biggest (8) first
    reply = next(m["text"] for m in _davis(db) if m["sender"] == "Karl")
    assert "sleeps 15" in reply and "two homes" in reply, text


def test_pay_zero_today_is_not_a_price(dc, db):
    _setup(dc, db, houses=False)
    h = _house(9)
    h.update(price_usd_per_night=None, price_raw="Pay $0 today")
    dc.post("/api/houses/ingest", json={"listings": [h]})
    _guest(db, f"Harare {_human(CI)} to {_human(CO)} for 2")
    r = _run(dc)
    assert r["plan"]["stage"] == "searching"                           # live price asked, not "$0" quoted
    assert "Pay $0" not in " ".join(m["text"] or "" for m in _davis(db))


def test_the_same_ask_is_never_sent_twice_word_for_word(dc, db):
    _setup(dc, db)
    for i in range(3):
        _guest(db, "Hi")
        _run(dc)
    texts = [m["text"] for m in _davis(db) if m["sender"] == "Karl"]
    assert len(texts) == 3 and len(set(texts)) == 3 and sum("I'm Karl from" in t for t in texts) == 1
    assert texts[2].startswith("Sorry, I didn't catch that")


def test_karl_muzunze_is_a_person_not_the_assistant():
    from app.davis import gate
    assert gate.is_davis("Karl") and gate.is_davis("Karl (catalogue)") and gate.is_davis("Davis")
    assert not gate.is_davis("Karl Muzunze") and not gate.is_davis("Front Desk")
    assert gate.is_reply_sender("Davis") and gate.is_reply_sender("Karl") and not gate.is_reply_sender("Karl (catalogue)")


def test_transcript_labels_cards_and_drops_noise():
    from app.davis import mind
    msgs = [
        {"direction": "in", "sender": "Guest", "kind": "text", "text": "Good day", "created_at": "2026-10-17T13:13:06+00:00"},
        {"direction": "in", "sender": "Guest", "kind": "text",
         "text": "You received a message on your phone, but your version of WhatsApp Web doesn't support it.",
         "created_at": "2026-10-17T13:55:10+00:00"},
        {"direction": "out", "sender": "Front Desk", "kind": "text",
         "text": "🏠 KCER 105\n📍 Bulawayo\n📅 Free 21 Nov 2026 → 22 Nov 2026", "created_at": "2026-10-18T19:45:25+00:00"},
        {"direction": "out", "sender": "Karl (catalogue)", "kind": "image", "text": "", "created_at": "2026-10-18T19:45:30+00:00"},
        {"direction": "out", "sender": "Karl (catalogue)", "kind": "image", "text": "", "created_at": "2026-10-18T19:45:31+00:00"},
        {"direction": "out", "sender": "You (WhatsApp)", "kind": "text", "text": "You deleted this message",
         "created_at": "2026-10-18T19:45:55+00:00"},
        {"direction": "out", "sender": "Davis", "kind": "text", "text": "Which KCER would you like?",
         "created_at": "2026-10-18T19:46:00+00:00"},
    ]
    lines = mind.transcript(msgs, tz_minutes=120)
    assert lines[0] == "[17 Oct 15:13] GUEST: Good day"
    assert not any("WhatsApp Web" in l for l in lines)
    assert any("TEAM: [house card KCER 105" in l for l in lines)
    assert sum("[photos]" in l for l in lines) == 1 and not any("deleted" in l for l in lines)
    assert lines[-1].endswith("KARL: Which KCER would you like?")


def test_booking_steps_use_the_plan_wording_and_questions_use_the_model(dc, db, monkeypatch):
    """On a slow local model the reply cascade keeps booking steps instant:
    the model writes only when the guest asked something."""
    _setup(dc, db)
    calls = _fake_model(monkeypatch, _understanding(city="harare", check_in=CI.isoformat(), check_out=CO.isoformat(),
                                                     guests=4), reply="unused")
    _karl(dc)
    _guest(db, f"Harare {_human(CI)} to {_human(CO)} for 4 people")
    r = _run(dc)
    assert r["plan"]["stage"] == "present" and r["plan"]["llm"]["skipped"] == "plan_wording_enough"
    assert not any(c["schema"].get("required") == ["reply"] for c in calls)
    _fake_model(monkeypatch, _understanding(city="harare", check_in=CI.isoformat(), check_out=CO.isoformat(), guests=4,
                                            open_questions=["is there parking"]),
                reply=lambda p: "Parking isn't in our listing, sorry. Which KCER would you like me to invoice?")
    _guest(db, "is there parking at these houses?")
    r = _run(dc)
    assert [a.get("by") for a in r["plan"]["actions"] if a["type"] == "reply"] == ["model"]


def test_karl_is_the_name_everywhere(dc, db):
    from app.davis import persona
    _setup(dc, db)
    _guest(db, "Hi")
    _run(dc)
    assert "I'm Karl from" in _davis(db)[-1]["text"] and _davis(db)[-1]["sender"] == "Karl"
    assert persona.PERSONA["name"] == "Karl" and "Davis" not in json.dumps(persona.LINES)


def test_party_size_and_questions_need_the_guests_words():
    """A small model invents polite questions and a party size; both are
    dropped unless the guest's own lines support them."""
    from app.davis import mind
    today = date(2026, 10, 18)
    g = ["Goodmorning", "House in Bulawayo 3 bedroom in 18 Dec and out 20 December 2026"]
    f = mind.to_features(_understanding(city="Bulawayo", guests=2, open_questions=["Do you need pictures?"]),
                         today, {}, [], None, g, g[-1:])
    assert "guests" not in f and f["open_questions"] == [] and f["city"] == "bulawayo"
    g2 = ["we are two couples", "May I kindly have pics", "And what amenties available"]
    f = mind.to_features(_understanding(guests=4, open_questions=["What amenities are available?"]),
                         today, {}, [], None, g2, g2[1:])
    assert f["guests"] == 4 and len(f["open_questions"]) == 2          # the dropped "pics" request is added back

