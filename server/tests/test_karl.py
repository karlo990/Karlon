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
    base = {"latest_need": "", "city": None, "areas": [], "check_in": None, "check_out": None, "dates_assumed": False,
            "guests": None, "bedrooms": None, "budget_usd": None, "budget_basis": None, "stay_type": "unknown",
            "chosen_ref": None, "photos_for": [], "wants_more_options": False, "full_name": None, "id_number": None,
            "open_questions": [], "intents": [], "mood": "neutral"}
    base.update(kw)
    return base


def _fake_claude(monkeypatch, understanding=None, reply=None, error=None):
    """Stand in for Claude: answers the understanding schema with
    `understanding` and the reply schema with `reply` (a string, or a
    function of the prompt). Records every call."""
    from app.davis import config, llm, mind
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test")
    calls = []

    async def complete(self, msgs, schema):
        calls.append({"system": msgs[0]["content"], "user": msgs[-1]["content"], "schema": schema})
        if error:
            raise llm.LLMError(error)
        if schema is mind.UNDERSTAND_SCHEMA or schema.get("required") == mind.UNDERSTAND_SCHEMA["required"]:
            u = understanding(msgs[-1]["content"]) if callable(understanding) else understanding
            return u or _understanding(), {"model": "claude-test"}
        text = reply(msgs[-1]["content"]) if callable(reply) else reply
        return {"reply": text or "Noted 👍"}, {"model": "claude-test"}
    monkeypatch.setattr(llm.ClaudeProvider, "complete", complete)
    return calls


def _points(prompt):
    """What the prompt says this message must get across (a well-behaved model keeps it)."""
    line = next(l for l in prompt.splitlines() if l.startswith("THIS MESSAGE MUST GET ACROSS"))
    return line.split(": ", 1)[1]


def _claude(dc):
    assert dc.put("/api/davis/settings", json={"provider": "claude", "llm_for": "ai"}).status_code == 200


# ─────────────────────────── understanding, from the chats ──────────────────

def test_dates_without_a_month_are_read_and_confirmed(dc, db, monkeypatch):
    """Rusape chat: "From 23 to 25 for two guests" was asked for again.
    Karl takes the dates, says it assumed them, and checks live homes."""
    _setup(dc, db, houses=False)
    calls = _fake_claude(monkeypatch, _understanding(
        city="rusape", check_in=CI.isoformat(), check_out=CO.isoformat(), dates_assumed=True, guests=2,
        stay_type="short_stay", latest_need="a home in Rusape for 2", intents=["booking"]),
        reply=lambda p: f"Lovely, Rusape for 2 guests, {_human(CI)} to {_human(CO)}: I'm checking live homes now. "
                        f"Is that the right month?")
    _claude(dc)
    _guest(db, "Greetings", 10)
    _staff(db, "Hello! I'm Karl from KarlCon Elite Retreats 🏡 Which city would you like to stay in, and for which dates?",
           9, sender="Karl")
    _guest(db, "Rusape", 8)
    _staff(db, "Lovely, Rusape! Which dates are you looking at, and for how many guests?", 7, sender="Karl")
    _guest(db, "From 23 to 25 for two guests")
    r = _run(dc)
    assert r["plan"]["stage"] == "searching" and r["refresh_job_id"]
    job = dc.get(f"/api/houses/refresh/{r['refresh_job_id']}").json()
    assert (job["location"], job["check_in"], job["check_out"]) == ("rusape", CI.isoformat(), CO.isoformat())
    understand, write = calls[0], calls[1]
    assert "From 23 to 25 for two guests" in understand["user"] and "GUEST: Rusape" in understand["user"]
    assert "THE DATES WERE ASSUMED" in write["user"]
    assert write["user"].rstrip().endswith("words).") and write["user"].index("NEWEST MESSAGES") > \
        write["user"].index("CONVERSATION")                       # the newest message goes last
    assert r["plan"]["actions"][0]["by"] == "model" and "Rusape" in _davis(db)[-1]["text"]


def test_questions_are_answered_first_and_photos_sent(dc, db, monkeypatch):
    """Bulawayo chat: "May I kindly have pics / what amenities / where is it
    located" got a generic line. Karl sends the photos and the model gets
    the questions as the last thing it reads."""
    _setup(dc, db)
    _fake_claude(monkeypatch, _understanding(city="harare", check_in=CI.isoformat(), check_out=CO.isoformat(),
                                              guests=4))
    _claude(dc)
    _guest(db, f"Harare {_human(CI)} to {_human(CO)} for 4")
    _run(dc)
    calls = _fake_claude(monkeypatch, _understanding(
        city="harare", check_in=CI.isoformat(), check_out=CO.isoformat(), guests=4, photos_for=[102],
        open_questions=["send pictures of KCER 102", "what amenities does it have", "where is it located"],
        intents=["photos", "amenities", "location"]),
        reply="Here are more photos of KCER 102 📸 It sleeps 8 in Greendale; the listing doesn't list amenities. "
              "Shall I reserve it for you?")
    _guest(db, "May I kindly have pics of KCER 102")
    _guest(db, "And what amenities available")
    _guest(db, "Where is this located")
    r = _run(dc)
    sends = [a for a in r["plan"]["actions"] if a["type"] == "send_options"]
    assert sends and sends[0]["max_images"] == 8 and [o["ref"] for o in sends[0]["options"]] == ["KCER 102"]
    prompt = calls[-1]["user"]
    assert prompt.index("ANSWER THESE FIRST") > prompt.index("CONVERSATION")
    assert "1. send pictures of KCER 102" in prompt and "3. where is it located" in prompt
    said = [m["text"] for m in _davis(db) if m["sender"] == "Karl"][-1]
    assert said.startswith("Here are more photos of KCER 102") and any(m["kind"] == "image" for m in _davis(db)[-6:])


def test_name_on_its_own_line_is_kept(dc, db, monkeypatch):
    """Bulawayo chat: "Blessing Nyasha Mpofu" was answered with "Which city"."""
    _setup(dc, db)
    _fake_claude(monkeypatch, _understanding(city="harare", check_in=CI.isoformat(), check_out=CO.isoformat(),
                                              guests=4))
    _claude(dc)
    _guest(db, f"Harare {_human(CI)} to {_human(CO)} for 4")
    _run(dc)
    _fake_claude(monkeypatch, _understanding(city="harare", check_in=CI.isoformat(), check_out=CO.isoformat(),
                                              guests=4, chosen_ref=102, intents=["booking"]),
                 reply=_points)
    _guest(db, "KCER 102")
    r = _run(dc)
    assert "plan" in r, r
    assert r["plan"]["stage"] == "details"
    _fake_claude(monkeypatch, _understanding(city="harare", check_in=CI.isoformat(), check_out=CO.isoformat(),
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
    _fake_claude(monkeypatch, error="claude: connection")
    _claude(dc)
    _guest(db, f"Harare {_human(CI)} to {_human(CO)} for 4")
    r = _run(dc)                                   # live: the reply is retried shortly, nothing lost
    assert r["status"] == "retry"
    r = _run(dc, again=True)
    r = _run(dc, again=True)
    assert r["status"] == "sent" and r["plan"]["stage"] == "present"     # rules read the message; plan wording


def test_claude_request_shape(monkeypatch):
    import asyncio
    from app.davis import config, llm, mind
    seen = {}

    class Messages:
        async def create(self, **kw):
            seen.update(kw)
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text='{"reply": "Hi"}')],
                                   usage=SimpleNamespace(input_tokens=900, output_tokens=12, cache_read_input_tokens=800))
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test")
    p = llm.ClaudeProvider("qwen2.5:3b-instruct")
    p._client = SimpleNamespace(messages=Messages())
    out, meta = asyncio.run(p.complete([{"role": "system", "content": "SYS"}, {"role": "user", "content": "U"}],
                                       mind.REPLY_SCHEMA))
    assert out == {"reply": "Hi"} and meta["cache_read_tokens"] == 800
    assert seen["model"] == config.KARL_MODEL                     # an Ollama model name never reaches Claude
    assert seen["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert seen["output_config"]["effort"] == config.KARL_EFFORT
    assert seen["output_config"]["format"] == {"type": "json_schema", "schema": mind.REPLY_SCHEMA}
    assert "thinking" not in seen and seen["messages"] == [{"role": "user", "content": "U"}]


# ─────────────────────────── without a model: the rules, fixed ──────────────

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


def test_karl_is_the_name_everywhere(dc, db):
    from app.davis import persona
    _setup(dc, db)
    _guest(db, "Hi")
    _run(dc)
    assert "I'm Karl from" in _davis(db)[-1]["text"] and _davis(db)[-1]["sender"] == "Karl"
    assert persona.PERSONA["name"] == "Karl" and "Davis" not in json.dumps(persona.LINES)


def test_claude_without_a_key_answers_with_the_rules_at_once(dc, db, monkeypatch):
    from app.davis import config
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    _setup(dc, db)
    _claude(dc)
    _guest(db, f"Harare {_human(CI)} to {_human(CO)} for 4")
    r = _run(dc)
    assert r["status"] == "sent" and r["plan"]["stage"] == "present"       # no retries waiting on a missing key
    assert dc.get("/api/davis/status").json()["claude"]["configured"] is False
