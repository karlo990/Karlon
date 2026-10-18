"""Davis: message analysis, the answer-or-not gate, the booking flow on the
real outbox (options with photos, invoice, Terms PDF), shadow drafts,
live-price resume, and the local model behind the guard."""
import json
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from app.wa_clean import chat_slug

GUEST = chat_slug("Leon")
TODAY = datetime.now(timezone.utc).date()
CI, CO = TODAY + timedelta(days=20), TODAY + timedelta(days=23)


def _human(d: date) -> str:
    return f"{d.day} {d.strftime('%B')}"


@pytest.fixture()
def davis_env(monkeypatch):
    monkeypatch.setenv("DAVIS_MODE", "live")
    monkeypatch.setenv("DAVIS_PROVIDER", "rules")
    monkeypatch.setenv("DAVIS_DEBOUNCE_SEC", "0")
    monkeypatch.setenv("DAVIS_SWEEP_SEC", "3600")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DAVIS_ADMIN_TOKEN", raising=False)


@pytest.fixture()
def dc(davis_env, client, monkeypatch):
    """The test client, unlocked with the Karlon password (the dashboard's lock)."""
    import app.auth as auth
    monkeypatch.setattr(auth, "PASSWORD", "test-pass")
    client.headers["X-Davis-Token"] = "test-pass"
    return client


def _house(n, hood="Borrowdale", usd=100.0, cap="6 guests · 3 bedrooms", city="Harare", ci=CI, co=CO, job=None):
    return {"url": f"https://www.airbnb.com/rooms/{n}", "title": f"Airbnb Title {n}", "location": city,
            "neighbourhood": hood, "capacity": cap, "check_in": ci.isoformat(), "check_out": co.isoformat(),
            "nights": (co - ci).days, "price_currency": "USD", "price_usd_per_night": usd, "rating": "4.8",
            "refresh_job_id": job}


def _setup(client, db, houses=True):
    db.execute("INSERT INTO chats (id, name, avatar_emoji, created_at, wa_name) VALUES (?,?,?,?,?)",
               (GUEST, "Leon", "LE", "2026-10-01T00:00:00+00:00", "Leon"))
    db.commit()
    if houses:
        client.post("/api/houses/ingest", json={"listings": [
            _house(1, usd=100.0), _house(2, hood="Greendale", usd=128.5, cap="8 guests · 5 bedrooms"),
            _house(3, hood="Avondale", usd=90.0, cap="2 guests · 1 bedroom"), _house(4, usd=150.0)]})
        db.execute("UPDATE house_listings SET images=?", (json.dumps([f"/static/houses/x/{i}.jpg" for i in range(5)]),))
        db.commit()


def _guest(db, text, minutes_ago=0.0, kind="text", sender="Leon"):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="microseconds")
    db.execute("INSERT INTO messages (id, chat_id, sender, kind, text, created_at, external_key, direction) "
               "VALUES (?,?,?,?,?,?,?, 'in')", (str(uuid.uuid4()), GUEST, sender, kind, text, ts, str(uuid.uuid4())))
    db.commit()


def _run(client, **params):
    r = client.post(f"/api/davis/sessions/{GUEST}/run", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _davis(db, since=None):
    rows = db.execute("SELECT * FROM messages WHERE chat_id=? AND sender LIKE 'Davis%' ORDER BY rowid",
                      (GUEST,)).fetchall()
    return [dict(r) for r in rows]


def _words(t):
    return len((t or "").split())


# ─────────────────────────── analysis ────────────────────────────────────────

def test_message_analysis():
    from app.davis import nlu
    t = date(2026, 10, 17)
    f = nlu.extract("Hi, looking for a house in Borrowdale for 4 people 11-14 Dec", t)
    assert (f["city"], f["guests"], f["check_in"], f["check_out"]) == ("harare", 4, "2026-12-11", "2026-12-14")
    assert f["greeting"] and f["availability_query"]
    f = nlu.extract("December 11th till 8th January", t)
    assert (f["check_in"], f["check_out"]) == ("2026-12-11", "2027-01-08")
    assert nlu.extract("from 20/12 to 27/12, 2 adults 3 kids", t)["guests"] == 5
    f = nlu.extract("Lynford Takudzwa Tapfumaneyi 63-1234567 X 42", t)
    assert f["id_number"] == "63-1234567 X 42" and f["name_like"] == "Lynford Takudzwa Tapfumaneyi"
    assert nlu.extract("how much is KCER 248?", t)["option_ref"] == 248
    assert nlu.extract("I will take the second one", t)["option_no"] == 2
    assert nlu.extract("first time in Harare", t).get("option_no") is None
    assert "check_in" not in nlu.extract("USD 12.50 per night, 2.5 baths", t)
    assert nlu.extract("STOP", t)["opt_out"] and nlu.extract("can I speak to a manager", t)["human_request"]
    assert nlu.extract("0772123456", t).get("long_number") is None          # a phone number, not an ID
    assert nlu.extract("Vic Falls 3 nights from 1 Nov", t)["check_out"] == "2026-11-04"
    long = "one two three four five six seven eight nine ten. " * 6
    assert _words(nlu.clip_words(long, 40)) <= 40


def test_gate_reasons():
    from app.davis import gate
    now = datetime.now(timezone.utc)

    def m(direction, text, mins, sender="Leon", kind="text"):
        return {"id": str(uuid.uuid4()), "direction": direction, "sender": sender, "kind": kind, "text": text,
                "created_at": (now - timedelta(minutes=mins)).isoformat()}

    def ctx(msgs, **session):
        return {"chat_id": "leon", "now": now, "messages": msgs, "session": {"last_inbound_id": None, **session},
                "settings": {"mode": "live", "paused": False, "only_chats": [], "ignore_chats": []}}

    assert gate.decide(ctx([m("in", "hi", 1)]), {})["respond"]
    assert gate.decide(ctx([m("in", "hi", 120)]), {})["respond"]                       # 2 hours: still answered
    assert gate.decide(ctx([m("in", "hi", 4 * 24 * 60)]), {})["reason"] == "guest_message_too_old"   # 4 days
    staff_then_guest = [m("out", "Sure, sending now", 5, "You (WhatsApp)"), m("in", "ok?", 1)]
    assert gate.decide(ctx(staff_then_guest), {})["respond"]                          # Davis has the last word
    # Davis's own echo, a returning PDF bubble and photos are not staff
    echo = [m("out", "Here are 3 homes 🏡", 5, "Davis"), m("out", "Here are 3 homes", 5, "You (WhatsApp)"),
            m("out", "KARLCON_Invoice_KCER-2026-0001.pdf 1 page 80 kB", 4, "You (WhatsApp)"),
            m("out", None, 4, "You (WhatsApp)", "image"), m("in", "nice", 1)]
    assert gate.decide(ctx(echo), {})["respond"]
    assert gate.decide(ctx([m("in", "hi", 2, "Ann"), m("in", "hello", 1, "Bob")]), {})["reason"] == "group_chat"
    # an unsaved number written two ways by WhatsApp is still one person (seen live)
    assert gate.decide(ctx([m("in", "hi", 2, "+263786557599"), m("in", "hello", 1, "+263 78 655 7599")]), {})["respond"]
    assert gate.sender_key("0786557599") == gate.sender_key("+263 78 655 7599")
    assert gate.decide(ctx([m("out", "Here you go.", 3, "Davis"), m("in", "thanks", 1)]), {"ack_only": True})["reason"] \
        == "acknowledgement_only"
    assert gate.decide(ctx([m("out", "Which dates?", 3, "Davis"), m("in", "ok", 1)]), {"ack_only": True})["respond"]
    last = m("in", "hi", 1)
    assert gate.decide(ctx([last], last_inbound_id=last["id"]), {})["reason"] == "already_handled"
    assert gate.decide(ctx([m("in", "hi", 1)], handoff=True), {})["reason"] == "staff_took_over"
    assert gate.decide(ctx([m("in", "hi", 1)], attention=True), {})["respond"]     # an alert never mutes Davis


# ─────────────────────────── the booking, live ───────────────────────────────

def test_full_booking_flow_live(dc, db):
    _setup(dc, db)
    _guest(db, "Hi")
    r = _run(dc)
    assert r["status"] == "sent" and r["plan"]["stage"] == "qualify"
    first = _davis(db)
    assert len(first) == 1 and first[0]["wa_status"] == "pending" and first[0]["direction"] == "out"
    assert "city" in first[0]["text"].lower()

    _guest(db, f"Harare, {_human(CI)} to {_human(CO)} for 4 people")
    r = _run(dc)
    assert r["plan"]["stage"] == "present"
    msgs = _davis(db)[1:]
    texts = [m for m in msgs if m["sender"] == "Davis"]
    cards = [m for m in msgs if m["sender"] == "Davis (catalogue)" and m["kind"] == "text"]
    photos = [m for m in msgs if m["kind"] == "image"]
    assert len(texts) == 1 and len(cards) == 3 and len(photos) == 9           # 3 options × 3 photos
    assert texts[0]["created_at"] < cards[0]["created_at"]                   # intro first
    assert all(_words(m["text"]) <= 40 for m in texts + cards)
    assert "KCER 103" not in " ".join(c["text"] for c in cards)              # sleeps 2: not for 4 people
    assert "airbnb" not in " ".join(m["text"] or "" for m in msgs).lower()
    sends = db.execute("SELECT * FROM house_sends WHERE chat_id=? ORDER BY option_no", (GUEST,)).fetchall()
    assert [s["sent_by"] for s in sends] == ["Davis (catalogue)"] * 3 and all(s["offer_id"] for s in sends)

    _guest(db, "I'll take option 2")
    r = _run(dc)
    assert "plan" in r, r
    choice = r["plan"]["choice"]
    assert r["plan"]["stage"] == "details" and choice["option_no"] == 2
    reply = _davis(db)[-1]["text"]
    assert choice["ref"] in reply and "name" in reply.lower() and _words(reply) <= 40

    _guest(db, "Lynford Takudzwa Tapfumaneyi")
    r = _run(dc)
    assert r["plan"]["slots"]["full_name"] == "Lynford Takudzwa Tapfumaneyi"
    assert "ID" in _davis(db)[-1]["text"]

    _guest(db, "63-1234567 X 42")
    r = _run(dc)
    assert r["plan"]["stage"] == "invoice" and r["invoice_id"]
    inv = db.execute("SELECT * FROM invoices WHERE id=?", (r["invoice_id"],)).fetchone()
    assert (inv["guest_name"], inv["id_number"], inv["created_by"], inv["chat_id"]) == \
        ("Lynford Takudzwa Tapfumaneyi", "63-1234567 X 42", "Davis", GUEST)
    assert inv["property_name"] == choice["ref"] and inv["rate"] == choice["usd_per_night"]
    assert (inv["check_in"], inv["check_out"], inv["status"]) == (CI.isoformat(), CO.isoformat(), "pending")
    terms = db.execute("SELECT * FROM messages WHERE chat_id=? AND kind='document'", (GUEST,)).fetchall()
    assert len(terms) == 1 and terms[0]["wa_status"] == "documents" and terms[0]["sender"] == "Davis (documents)"

    _guest(db, "I have paid, here is the proof")
    r = _run(dc)
    s = dc.get(f"/api/davis/sessions/{GUEST}").json()["session"]
    assert s["attention"] and s["attention_reason"] == "payment proof to verify" and s["stage"] == "payment"
    assert not s["handoff"]
    _guest(db, "hello?")                                       # Davis still answers after payment
    assert _run(dc)["status"] == "sent" and "confirming your payment" in _davis(db)[-1]["text"]

    dc.post(f"/api/davis/sessions/{GUEST}/handoff", json={"active": True})   # staff take over: Davis muted
    _guest(db, "any update?")
    assert _run(dc)["reason"] == "staff_took_over"
    dc.post(f"/api/davis/sessions/{GUEST}/attention", json={"active": False})
    assert not dc.get(f"/api/davis/sessions/{GUEST}").json()["session"]["attention"]


def test_never_more_than_two_replies_per_guest_turn(dc, db):
    from app.davis import gate
    from app.davis.engine import Engine
    _setup(dc, db)
    _guest(db, "Hi")
    _run(dc)
    assert _run(dc, again="true")["reason"] == "last_message_not_from_guest"   # Davis spoke last: wait
    msgs = [{"direction": "in", "kind": "text", "text": "hi"},
            {"direction": "out", "sender": "Davis", "kind": "text", "text": "Hello! Which city?"}]
    two = [{"type": "reply", "text": "a"}, {"type": "send_options", "options": []}, {"type": "reply", "text": "b"}]
    assert [a["type"] for a in Engine._cap(two, msgs)] == ["reply", "send_options"]   # 1 already sent → 1 more
    msgs.append({"direction": "out", "sender": "Davis", "kind": "text", "text": "Checking prices now."})
    assert gate.davis_replies_this_turn(msgs) == 2 and Engine._cap(two, msgs) == [two[1]]


def test_old_messages_quiet_but_davis_answers_after_staff(dc, db, monkeypatch):
    _setup(dc, db)
    _guest(db, "Hi, any houses in Harare?", minutes_ago=4 * 24 * 60)       # older than 3 days
    assert _run(dc)["reason"] == "guest_message_too_old"
    db.execute("INSERT INTO messages (id, chat_id, sender, kind, text, created_at, direction, wa_status) "
               "VALUES (?,?,?,?,?,?, 'out', 'sent')",
               ("staff1", GUEST, "Front Desk", "text", "Hi Leon, which dates?",
                (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()))
    db.commit()
    _guest(db, f"{_human(CI)} to {_human(CO)}")
    assert _run(dc)["status"] == "sent" and _davis(db)                     # Davis has the last word
    from app.davis import config
    monkeypatch.setattr(config, "HUMAN_QUIET_MINUTES", 30)                 # optional quiet window still works
    _guest(db, "thanks, and for 4 people?")
    db.execute("INSERT INTO messages (id, chat_id, sender, kind, text, created_at, direction, wa_status) "
               "VALUES ('staff2', ?, 'Front Desk', 'text', 'Sure!', ?, 'out', 'sent')",
               (GUEST, datetime.now(timezone.utc).isoformat()))
    _guest(db, "ok great")
    assert _run(dc)["reason"] == "staff_replied_recently"


def test_opt_out_and_back(dc, db):
    _setup(dc, db)
    _guest(db, "STOP")
    _run(dc)
    assert "won't message" in _davis(db)[-1]["text"]
    _guest(db, "hello")
    assert _run(dc)["reason"] == "opted_out"
    _guest(db, "START")
    assert _run(dc)["status"] == "sent"


def test_complaint_hands_over_to_staff(dc, db):
    _setup(dc, db)
    _guest(db, "This is a scam, I want a refund")
    _run(dc)
    s = dc.get(f"/api/davis/sessions/{GUEST}").json()["session"]
    assert s["attention"] and s["attention_reason"] == "complaint" and not s["handoff"]
    assert "sorry" in _davis(db)[-1]["text"].lower()
    _guest(db, "The last house had no water")
    assert _run(dc)["status"] == "sent"                        # Davis stays with the guest


def test_hidden_and_featured_houses(dc, db):
    _setup(dc, db)
    assert dc.patch("/api/davis/catalogue", json={"listing_url": "https://www.airbnb.com/rooms/1",
                                                  "hidden": True}).status_code == 200
    dc.patch("/api/davis/catalogue", json={"listing_url": "https://www.airbnb.com/rooms/4", "featured": True})
    _guest(db, f"Harare {_human(CI)} to {_human(CO)}")
    r = _run(dc)
    refs = [o["url"][-1] for o in next(a for a in r["plan"]["actions"] if a["type"] == "send_options")["options"]]
    assert refs[0] == "4" and "1" not in refs
    cat = dc.get("/api/davis/catalogue", params={"city": "harare"}).json()
    assert {h["ref_no"] for h in cat["houses"]} == {101, 102, 103, 104} and "harare" in cat["cities"]
    assert next(h for h in cat["houses"] if h["url"].endswith("/4"))["featured"]


# ─────────────────────────── shadow mode ─────────────────────────────────────

def test_shadow_mode_drafts_then_staff_approve(dc, db):
    _setup(dc, db)
    assert dc.put("/api/davis/settings", json={"mode": "shadow"}).json()["mode"] == "shadow"
    _guest(db, f"Hello, a house in Harare {_human(CI)} to {_human(CO)} for 2?")
    r = _run(dc)
    assert r["status"] == "drafted" and _davis(db) == []
    drafts = dc.get("/api/davis/drafts").json()
    assert len(drafts) == 1 and drafts[0]["chat_id"] == GUEST

    # the guest wrote again: approving the old draft needs force
    _guest(db, "for 2 people please")
    assert dc.post(f"/api/davis/drafts/{drafts[0]['id']}/approve", json={}).status_code == 409
    ok = dc.post(f"/api/davis/drafts/{drafts[0]['id']}/approve", json={"text": "Here you go! Which KCER do you like?",
                                                                        "force": True})
    assert ok.status_code == 200 and ok.json()["replied"]
    sent = _davis(db)
    assert sent[0]["text"] == "Here you go! Which KCER do you like?"
    assert sum(1 for m in sent if m["sender"] == "Davis (catalogue)" and m["kind"] == "text") == 3


# ─────────────────────────── live prices (refresh) ───────────────────────────

def test_no_prices_for_dates_requests_live_prices_then_sends(dc, db):
    _setup(dc, db, houses=False)
    other = (TODAY + timedelta(days=40), TODAY + timedelta(days=42))
    dc.post("/api/houses/ingest", json={"listings": [_house(7, ci=other[0], co=other[1])]})
    _guest(db, f"Any place in Harare from {_human(CI)} to {_human(CO)}?")
    r = _run(dc)
    assert r["plan"]["stage"] == "searching" and r["refresh_job_id"]
    job = dc.get(f"/api/houses/refresh/{r['refresh_job_id']}").json()
    assert (job["location"], job["check_in"], job["check_out"]) == ("harare", CI.isoformat(), CO.isoformat())
    assert "Checking live availability" in _davis(db)[-1]["text"]

    dc.get("/api/houses/refresh/pending")
    dc.post("/api/houses/ingest", json={"listings": [_house(7, ci=CI, co=CO, job=job["id"]),
                                                     _house(8, ci=CI, co=CO, job=job["id"], usd=80.0)]})
    dc.post(f"/api/houses/refresh/{job['id']}/complete", json={"pushed": 2})
    for _ in range(50):                                   # the hook runs on the engine's loop
        if any(m["sender"] == "Davis (catalogue)" for m in _davis(db)):
            break
        time.sleep(0.1)
    msgs = _davis(db)
    assert sum(1 for m in msgs if m["sender"] == "Davis (catalogue)" and m["kind"] == "text") == 2
    assert sum(1 for m in msgs if m["sender"] == "Davis") == 2          # "checking" + "here are": the turn's cap
    assert dc.get(f"/api/davis/sessions/{GUEST}").json()["session"]["stage"] == "present"


# ─────────────────────────── hook from the bridge sync ───────────────────────

def test_bridge_sync_triggers_davis(dc, db):
    _setup(dc, db)
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    snap = {"schema": "karlon.chat_snapshot/1",
            "chat": {"id": GUEST, "name": "Leon", "wa_name": "Leon", "avatar_emoji": "LE"},
            "messages": [{"seq": 1, "external_key": "k1", "direction": "in", "sender": "Leon", "kind": "text",
                          "text": "Hello, do you have houses in Bulawayo?", "created_at": now}]}
    assert dc.post(f"/api/chats/{GUEST}/sync", json=snap).status_code == 200
    for _ in range(50):
        if _davis(db):
            break
        time.sleep(0.1)
    assert _davis(db) and "date" in _davis(db)[0]["text"].lower()


# ─────────────────────────── the model ───────────────────────────────────────

def _fake_local(monkeypatch, payload=None, error=None):
    """Stand in for the llama.cpp model: record each prompt, answer payload."""
    from app.davis import local_llm
    calls = []

    def generate(msgs, schema):
        calls.append({"system": msgs[0]["content"], "user": msgs[-1]["content"], "schema": schema})
        if error:
            raise RuntimeError(error)
        return payload, {"model": "test.gguf", "prompt_tokens": 300, "output_tokens": 30, "tokens_per_s": 9.0}
    monkeypatch.setattr(local_llm.model, "generate", generate)
    return calls


def test_local_model_prompt_and_guarded_reply(dc, db, monkeypatch):
    from app.davis import persona
    _setup(dc, db)
    calls = _fake_local(monkeypatch, {"reply": "Mhoro! Lovely, Harare! Which dates are you looking at, and for how many guests?",
                                      "handoff": False})
    assert dc.put("/api/davis/settings", json={"provider": "local", "llm_for": "all"}).status_code == 200
    _guest(db, "Hi, I need a place in Harare")
    r = _run(dc)
    c = calls[0]
    assert c["schema"]["required"] == ["reply", "handoff"] and c["schema"]["additionalProperties"] is False
    assert persona.PERSONA["name"] in c["system"] and "KCER" in c["system"]
    assert "KEY POINTS:" in c["user"] and "airbnb.com" not in c["user"]
    assert _davis(db)[-1]["text"].startswith("Mhoro!") and r["plan"]["llm"]["provider"] == "local"


def test_model_reply_with_invented_price_is_not_sent(dc, db, monkeypatch):
    _setup(dc, db)
    _fake_local(monkeypatch, {"reply": "Great news, all homes are just USD 20 a night! Which one?", "handoff": False})
    dc.put("/api/davis/settings", json={"provider": "local", "llm_for": "all"})
    _guest(db, f"Harare {_human(CI)} to {_human(CO)} for 4")
    r = _run(dc)
    assert r["plan"]["llm"]["rejected"].startswith(("number_not_in_facts", "price_not_in_facts"))
    assert "USD 20" not in _davis(db)[0]["text"] and "KCER" in _davis(db)[0]["text"]


def test_model_handoff_only_counts_on_open_questions(dc, db, monkeypatch):
    _setup(dc, db)
    _fake_local(monkeypatch, {"reply": "Hello! Which city and dates would you like?", "handoff": True})
    dc.put("/api/davis/settings", json={"provider": "local", "llm_for": "all"})
    _guest(db, "Hello")
    _run(dc)
    assert not dc.get(f"/api/davis/sessions/{GUEST}").json()["session"]["attention"]   # a greeting raises no alert


def test_model_down_falls_back_and_breaker_opens(dc, db, monkeypatch):
    _fake_local(monkeypatch, error="model file missing")
    dc.put("/api/davis/settings", json={"provider": "local", "llm_for": "all"})
    _setup(dc, db)
    for i in range(4):
        _guest(db, "Hi")
        _run(dc)
        assert "city" in _davis(db)[-1]["text"].lower()               # the persona's line still goes out
        db.execute("DELETE FROM messages WHERE sender LIKE 'Davis%'")
        db.execute("UPDATE davis_sessions SET last_inbound_id=NULL")
        db.commit()
    st = dc.get("/api/davis/status").json()
    assert st["llm"]["breakers"]["local"]["open"] and st["llm"]["stats"]["breaker_skips"] >= 1
    assert st["local_model"]["file"].endswith(".gguf")


def test_guard_catches_small_model_mistakes():
    from app.davis import policy
    facts = {"slots": {"city": "harare"}, "sending_now": [{"ref": "KCER 101", "ref_no": 101, "usd_per_night": 140.0,
                                                          "usd_total": 420.0, "nights": 3}]}
    draft = "Here are 1 home in Harare 🏡 Reply with the KCER number you like and I'll prepare your invoice."
    assert policy.check_reply("Hello Davis! I'd like to stay in Victoria Falls.", facts, draft) == "wrote_as_guest"
    assert policy.check_reply("Lovely, from 15 to 19 October?", facts, draft).startswith("number_not_in_facts")
    ok = "KCER 101 in Harare is USD 140 a night, USD 420 in total. Shall I invoice it?"
    assert policy.check_reply(ok, facts, "Great choice: KCER 101, USD 420 total. Name and ID?", keep_draft=True) is None
    assert policy.check_reply("Great choice! Send your name and ID?", facts,
                              "Great choice: KCER 101, USD 420 total. Name and ID?", keep_draft=True) == "dropped:KCER 101"


def test_open_question_uses_house_note_or_hands_over(dc, db, monkeypatch):
    _setup(dc, db)
    calls = _fake_local(monkeypatch, {"reply": "Yes, KCER 102 has wifi and a pool ✨ Shall I prepare your invoice?",
                                      "handoff": False})
    dc.put("/api/davis/settings", json={"provider": "local", "llm_for": "open"})    # model for open questions only
    _guest(db, f"Harare {_human(CI)} to {_human(CO)} for 4")
    _run(dc)
    assert calls == []                                                          # booking steps: persona lines, no model
    _guest(db, "is there wifi?")
    _run(dc)
    assert calls == [] and dc.get(f"/api/davis/sessions/{GUEST}").json()["session"]["attention"]   # no note: team
    dc.post(f"/api/davis/sessions/{GUEST}/attention", json={"active": False})
    dc.patch("/api/davis/catalogue", json={"listing_url": "https://www.airbnb.com/rooms/2", "note": "wifi, pool"})
    _guest(db, "does KCER 102 have wifi?")
    r = _run(dc)
    assert len(calls) == 1 and "note: wifi, pool" in calls[0]["user"]
    assert _davis(db)[-1]["text"].startswith("Yes, KCER 102 has wifi")
    assert not dc.get(f"/api/davis/sessions/{GUEST}").json()["session"]["attention"]

    _guest(db, "is KCER 102 near the airport?")              # no note mentions it: never ask the model
    _run(dc)
    assert len(calls) == 1 and dc.get(f"/api/davis/sessions/{GUEST}").json()["session"]["attention"]


def test_model_answer_must_use_the_note(dc, db, monkeypatch):
    _setup(dc, db)
    _fake_local(monkeypatch, {"reply": "Yes, it's close to everything ✨ Shall I prepare your invoice?", "handoff": False})
    dc.put("/api/davis/settings", json={"provider": "local", "llm_for": "open"})
    dc.patch("/api/davis/catalogue", json={"listing_url": "https://www.airbnb.com/rooms/2", "note": "wifi, pool"})
    _guest(db, f"Harare {_human(CI)} to {_human(CO)} for 4")
    _run(dc)
    _guest(db, "does KCER 102 have a pool for the kids?")
    r = _run(dc)
    assert r["plan"]["llm"]["rejected"] == "answer_not_grounded_in_note"
    assert _davis(db)[-1]["text"].startswith("KCER 102 has wifi, pool")            # the note itself goes out
    assert not dc.get(f"/api/davis/sessions/{GUEST}").json()["session"]["attention"]


def test_question_while_collecting_details(dc, db):
    _setup(dc, db)
    _guest(db, f"Harare {_human(CI)} to {_human(CO)} for 4")
    _run(dc)
    _guest(db, "KCER 102")
    _run(dc)
    _guest(db, "can we bring our dog?")
    _run(dc)
    text = _davis(db)[-1]["text"]
    assert "I'll check that for you" in text and "team" not in text           # no "ask the team" excuse
    assert text.endswith("Please send your full name and ID number for the invoice.")
    assert dc.get(f"/api/davis/sessions/{GUEST}").json()["session"]["attention"]   # the team is alerted quietly


def test_choice_and_name_in_one_burst_are_both_kept(dc, db):
    _setup(dc, db)
    _guest(db, f"Harare {_human(CI)} to {_human(CO)} for 4")
    _run(dc)
    _guest(db, "how much is the second one?")
    _run(dc)                                                   # "Shall I reserve it for you?"
    _guest(db, "yes")
    _guest(db, "Tendai Moyo")                                  # one turn: the choice and the name
    r = _run(dc)
    assert r["plan"]["stage"] == "details" and r["plan"]["slots"]["full_name"] == "Tendai Moyo"
    _guest(db, "63-2345678 Q 12")
    assert _run(dc)["plan"]["stage"] == "invoice"


def test_ecocash_number_is_given_and_no_other(dc, db, monkeypatch):
    from app.davis import policy
    _setup(dc, db)
    _guest(db, f"Harare {_human(CI)} to {_human(CO)} for 4")
    _run(dc)
    _guest(db, "what is your ecocash number?")
    _run(dc)
    assert "EcoCash 0780563561 (Karl Muzunze)" in _davis(db)[-1]["text"]
    assert not dc.get(f"/api/davis/sessions/{GUEST}").json()["session"]["attention"]    # not a payment claim
    for t in ("KCER 102", "Leon Moyo 63-1234567 X 42"):
        _guest(db, t)
        r = _run(dc)
    sent = [m["text"] for m in _davis(db) if m["sender"] == "Davis"]
    assert r["plan"]["stage"] == "invoice" and "EcoCash 0780563561 (Karl Muzunze)" in sent[-1]
    _guest(db, "where do I pay?")
    _run(dc)
    assert "0780563561" in _davis(db)[-1]["text"]
    # a model reply giving any other number is never sent
    facts = r["plan"]["facts"]
    assert policy.check_reply("Pay by EcoCash 0771234567 please.", facts).startswith("number_not_in_facts")
    assert policy.check_reply("Pay by EcoCash 0780563561 (Karl Muzunze), then send proof here.", facts) is None


def test_greeting_comes_before_the_payment_line(dc, db):
    _setup(dc, db)
    _guest(db, "Hi, what is your ecocash number?")
    _run(dc)
    text = _davis(db)[-1]["text"]
    assert text.startswith("Hello Leon!") and "EcoCash 0780563561 (Karl Muzunze)" in text and _words(text) <= 40


def test_ignore_list_from_space_variable_survives_restarts(dc, monkeypatch, db):
    client = dc
    from app.davis import config
    monkeypatch.setattr(config, "IGNORE_CHATS", [GUEST])
    _setup(client, db)
    _guest(db, "Hi")
    r = client.post(f"/api/davis/sessions/{GUEST}/run").json()
    assert r["reason"] == "ignored_chat" and _davis(db) == []
    client.put("/api/davis/settings", json={"ignore_chats": ["wa-other"]})
    assert set(client.get("/api/davis/status").json()["settings"]["ignore_chats"]) == {GUEST, "wa-other"}


def test_viewing_request_is_answered_not_handed_over(dc, db):
    """Taku's chat (live): staff sent options, then "where do I pay" and "I
    need to see the place first" sat unanswered behind a handoff."""
    _setup(dc, db)
    dc.post("/api/houses/send", json={"chat_id": GUEST, "listing_ids": ["https://www.airbnb.com/rooms/2"],
                                      "sender": "Front Desk"})
    db.execute("UPDATE messages SET created_at=? WHERE sender='Front Desk'",
               ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),))
    db.commit()
    _guest(db, "Okay where do I pay")
    _guest(db, "Also I need to see the place first")
    r = _run(dc)
    text = _davis(db)[-1]["text"]
    assert "EcoCash 0780563561" in text and "Viewings aren't possible before a reservation" in text
    assert "live location and host address" in text and text.rstrip().endswith("?") and _words(text) <= 40
    assert not any(a["type"] == "alert_staff" for a in r["plan"]["actions"])
    _guest(db, "what time is check in?")
    _run(dc)
    assert "Check-in is from 2:00 PM" in _davis(db)[-1]["text"]


def test_a_failed_pass_is_retried_not_lost(dc, db, monkeypatch):
    from app.davis import policy
    _setup(dc, db)
    real = policy.plan
    monkeypatch.setattr(policy, "plan", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    _guest(db, "Hi")
    with pytest.raises(RuntimeError):
        dc.post(f"/api/davis/sessions/{GUEST}/run")
    monkeypatch.setattr(policy, "plan", real)
    assert _run(dc)["status"] == "sent"                       # the guest still gets an answer


def test_skills_learned_from_team_replies_in_other_chats(dc, db):
    from app.davis import skills
    _setup(dc, db)
    other = "wa-other"
    db.execute("INSERT INTO chats (id, name, avatar_emoji, created_at) VALUES (?,?,?,?)", (other, "Nomsa Dube", "ND", "2026-10-01"))
    now = datetime.now(timezone.utc)
    rows = [("in", "Nomsa Dube", "Do you allow pets in the house?"),
            ("out", "You (WhatsApp)", "Yes, small pets are welcome, just let us know before arrival."),
            ("in", "Nomsa Dube", "Is there parking for two cars?"),
            ("out", "You (WhatsApp)", "Hi Nomsa, yes there is space for 2 cars."),     # a digit and her name: not learned
            ("in", "Nomsa Dube", "Can we have a braai outside?"),
            ("out", "Davis", "Sure, I'll check that for you.")]                          # Davis's own words: not learned
    for i, (d, who, text) in enumerate(rows):
        db.execute("INSERT INTO messages (id, chat_id, sender, kind, text, created_at, direction) VALUES (?,?,?,?,?,?,?)",
                   (f"o{i}", other, who, "text", text, (now - timedelta(hours=5) + timedelta(minutes=i)).isoformat(), d))
    db.commit()
    assert skills.learn(db) == 1
    learned = dc.get("/api/davis/skills").json()
    assert [k["answer"] for k in learned] == ["Yes, small pets are welcome, just let us know before arrival."]

    _guest(db, f"Harare {_human(CI)} to {_human(CO)} for 4")
    _run(dc)
    _guest(db, "are pets allowed in the house?")
    r = _run(dc)
    text = _davis(db)[-1]["text"]
    assert text.startswith("Yes, small pets are welcome") and text.endswith("?") and r["plan"]["intent"] == "skill"
    assert not dc.get(f"/api/davis/sessions/{GUEST}").json()["session"]["attention"]
    assert dc.get("/api/davis/skills").json()[0]["uses"] == 1
    k = dc.post("/api/davis/skills", json={"question": "Is there a generator for power cuts?",
                                           "answer": "Our homes have solar or generator backup for power cuts."}).json()
    assert k["source"] == "manual"
    dc.patch(f"/api/davis/skills/{k['id']}", json={"enabled": False})
    _guest(db, "do you have a generator for power cuts?")
    _run(dc)
    assert "solar" not in _davis(db)[-1]["text"]                   # switched off: not used


def test_business_faq_and_small_talk_need_no_staff(dc, db):
    _setup(dc, db)
    _guest(db, f"Harare {_human(CI)} to {_human(CO)} for 4")
    _run(dc)
    _guest(db, "Can I confirm your Reg number")
    _run(dc)
    text = _davis(db)[-1]["text"]
    assert "Reg. No. 15017/2023" in text and text.endswith("?") and "check" not in text
    _guest(db, "Hi")
    _guest(db, "How are yu")
    _run(dc)
    text = _davis(db)[-1]["text"]
    assert text.startswith("I'm well, thank you Leon!") and "check" not in text
    assert not dc.get(f"/api/davis/sessions/{GUEST}").json()["session"]["attention"]


def test_quoted_total_matches_the_invoice_with_service_fee(dc, db):
    from app.davis import catalogue
    assert catalogue.amount_due(128.5, 3) == 443.33                # 385.50 + 15% (57.83), as invoice_worker rounds
    _setup(dc, db)
    _guest(db, f"Harare {_human(CI)} to {_human(CO)} for 4")
    _run(dc)
    _guest(db, "KCER 102")
    _run(dc)
    due = catalogue.amount_due(128.5, (CO - CI).days)
    assert f"USD {due:,.2f}".replace(".00", "") in _davis(db)[-1]["text"] and "15% service fee" in _davis(db)[-1]["text"]


def test_bulawayo_chat_dates_from_context_and_staff(dc, db):
    """Live chat: the guest stated a check-out date (no space before the
    month), staff confirmed "21 to 22", the guest said 15 guests; Davis must
    not answer with check-in times or ask for dates again."""
    _setup(dc, db, houses=False)
    _guest(db, "Hi, a place in Bulawayo please")
    _run(dc)
    _guest(db, "check out time 10:00am 22November")
    r = _run(dc)
    assert "Check-in is from" not in _davis(db)[-1]["text"]
    assert r["plan"]["slots"]["check_out"].endswith("-11-22")
    db.execute("INSERT INTO messages (id, chat_id, sender, kind, text, created_at, direction, wa_status) "
               "VALUES ('s1', ?, 'You (WhatsApp)', 'text', 'Let me use the ones you sent i.e 21 to 22', ?, 'out', NULL)",
               (GUEST, datetime.now(timezone.utc).isoformat()))
    db.commit()
    _guest(db, "15guests..")
    r = _run(dc)
    slots = r["plan"]["slots"]
    assert slots["check_in"].endswith("-11-21") and slots["check_out"].endswith("-11-22") and slots["guests"] == 15
    assert "Which dates" not in _davis(db)[-1]["text"]


def test_ai_only_mode_model_writes_and_never_sends_fixed_sentences(dc, db, monkeypatch):
    from app.davis import local_llm
    _setup(dc, db)
    dc.put("/api/davis/settings", json={"provider": "local", "llm_for": "ai"})
    answers = iter([{"reply": "Mhoro Leon! Harare is a great choice. What dates are you thinking of, and how many "
                              "of you are coming?", "handoff": False}])
    calls = []

    def gen(msgs, schema):
        calls.append(msgs[-1]["content"])
        return next(answers), {"model": "t.gguf"}
    monkeypatch.setattr(local_llm.model, "generate", gen)
    _guest(db, "Hi, I need a place in Harare")
    r = _run(dc)
    assert r["status"] == "sent" and _davis(db)[-1]["text"].startswith("Mhoro Leon! Harare is a great choice")
    assert "KEY POINTS:" in calls[0]

    # a reply that drops the payment number is retried, then left to staff: nothing canned is sent
    bad = {"reply": "Please pay by EcoCash and send proof here.", "handoff": False}
    monkeypatch.setattr(local_llm.model, "generate", lambda msgs, schema: (calls.append(msgs[-1]["content"]) or bad,
                                                                          {"model": "t.gguf"}))
    before = len(_davis(db))
    _guest(db, "how do I pay?")
    r = _run(dc)
    assert r["status"] == "needs_staff" and len(_davis(db)) == before
    assert "REJECTED (dropped:" in calls[-1]                                    # the retry was told why
    s = dc.get(f"/api/davis/sessions/{GUEST}").json()["session"]
    assert s["attention"] and "couldn't write a safe reply" in s["attention_reason"]
    drafts = dc.get("/api/davis/drafts").json()
    assert drafts and [a["type"] for a in drafts[0]["plan"]["actions"]] == ["reply"]   # staff can send/edit it

    # model down: still no fixed sentence to the guest
    def boom(msgs, schema):
        raise RuntimeError("model file missing")
    monkeypatch.setattr(local_llm.model, "generate", boom)
    _guest(db, "hello?")
    assert _run(dc)["status"] == "needs_staff" and len(_davis(db)) == before


def test_persona_lines_fit_the_word_limit():
    from app.davis import persona
    sample = dict(hi=" Tendai", city="Victoria Falls", when="28 Dec–2 Jan", party=" for 12 guests", k=3,
                  homes="homes", are="are", ref="KCER 1234", place="Mount Pleasant Heights, Harare",
                  price=", USD 1,250.50 per night, USD 6,252.50 for 5 nights",
                  summary="KCER 1234, 28 Dec–2 Jan, USD 6,252.50 total", money="USD 6,252.50",
                  total_word=" total", start="28 Dec",
                  notes="KCER 1234 has wifi, pool, solar backup; KCER 1235 has wifi, borehole",
                  next="Please send your full name and ID number for the invoice.")
    for key in persona.LINES:
        assert _words(persona.line(key, **sample)) <= 40, key


# ─────────────────────────── dashboard API ───────────────────────────────────

def test_preview_status_and_admin_token(dc, db, monkeypatch):
    _setup(dc, db)
    p = dc.post("/api/davis/preview", json={"text": f"Harare {_human(CI)} to {_human(CO)} for 4 people"}).json()
    assert p["stage"] == "present" and [a["type"] for a in p["actions"]] == ["reply", "send_options"]
    assert _davis(db) == []                                                     # preview sends nothing
    st = dc.get("/api/davis/status").json()
    assert st["settings"]["mode"] == "live" and st["limits"]["max_words"] == 40
    page = dc.get("/davis")
    assert page.status_code == 200 and "/api/davis/ws" in page.text

    assert dc.put("/api/davis/settings", json={"mode": "off"}).status_code == 200
    _guest(db, "Hi")
    r = dc.post(f"/api/davis/sessions/{GUEST}/run").json()
    assert r["status"] == "drafted" and _davis(db) == []          # mode off: "run now" only drafts


def test_dashboard_is_locked_by_the_karlon_password(dc, db, monkeypatch):
    import app.auth as auth
    _setup(dc, db)
    del dc.headers["X-Davis-Token"]
    page = dc.get("/davis")
    assert page.status_code == 200 and 'class="locked"' in page.text and "Davis is locked" in page.text
    for path in ("/api/davis/status", "/api/davis/sessions", "/api/davis/drafts", "/api/davis/events",
                 f"/api/davis/sessions/{GUEST}", "/api/davis/catalogue"):
        assert dc.get(path).status_code == 401, path                 # no guest data without the password
    assert dc.put("/api/davis/settings", json={"mode": "off"}).status_code == 401
    assert dc.post("/api/davis/preview", json={"text": "hi"}).status_code == 401
    assert dc.get("/api/davis/lock-state").json() == {"configured": True, "unlocked": False}
    assert dc.post("/api/davis/unlock", json={"password": "nope"}).status_code == 401
    ok = dc.post("/api/davis/unlock", json={"password": "test-pass"})
    assert ok.status_code == 200 and "davis_unlock=" in ok.headers["set-cookie"] and "HttpOnly" in ok.headers["set-cookie"]
    cookie = ok.headers["set-cookie"].split(";")[0]
    assert dc.get("/api/davis/status", headers={"cookie": cookie}).status_code == 200
    assert dc.get("/api/davis/status", headers={"cookie": "davis_unlock=forged"}).status_code == 401
    with pytest.raises(Exception):                                   # the live feed refuses too
        with dc.websocket_connect("/api/davis/ws") as ws:
            ws.receive_json()
    with dc.websocket_connect("/api/davis/ws", headers={"cookie": cookie}) as ws:
        assert ws.receive_json()["type"] == "hello"
    assert dc.get("/api/chats").status_code == 200                   # the app and bridge APIs stay open
    monkeypatch.setattr(auth, "PASSWORD", "")                         # no password set: stays locked
    assert dc.get("/api/davis/status").status_code == 403
    assert dc.get("/api/davis/lock-state").json()["configured"] is False


def test_cut_off_reply_keeps_complete_sentences():
    from app.davis.local_llm import salvage
    cut = '{"reply": "Full payment confirms the booking for KCER 5100, 11–14 Dec. Please send USD 520 to EcoCash 07805'
    assert salvage(cut) == {"reply": "Full payment confirms the booking for KCER 5100, 11–14 Dec.", "handoff": False}
    assert salvage('{"reply": "Hello there, I can help you with') is None          # no full sentence
    assert salvage('{"reply": "Sure. Thanks a lot for that, I will check it now.\\') == {
        "reply": "Sure. Thanks a lot for that, I will check it now.", "handoff": False}
    assert salvage('{"handoff": tr') is None
