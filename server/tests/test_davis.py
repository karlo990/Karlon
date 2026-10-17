"""Davis: message analysis, the answer-or-not gate, the booking flow on the
real outbox (options with photos, invoice, Terms PDF), shadow drafts,
live-price resume, and the Claude request it sends."""
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
def dc(davis_env, client):
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
    assert gate.decide(ctx([m("in", "hi", 120)]), {})["reason"] == "guest_message_too_old"
    assert gate.decide(ctx([m("out", "Sure, sending now", 5, "You (WhatsApp)"), m("in", "ok?", 1)]), {})["reason"] \
        == "staff_replied_recently"
    # Davis's own echo, a returning PDF bubble and photos are not staff
    echo = [m("out", "Here are 3 homes 🏡", 5, "Davis"), m("out", "Here are 3 homes", 5, "You (WhatsApp)"),
            m("out", "KARLCON_Invoice_KCER-2026-0001.pdf 1 page 80 kB", 4, "You (WhatsApp)"),
            m("out", None, 4, "You (WhatsApp)", "image"), m("in", "nice", 1)]
    assert gate.decide(ctx(echo), {})["respond"]
    assert gate.decide(ctx([m("in", "hi", 2, "Ann"), m("in", "hello", 1, "Bob")]), {})["reason"] == "group_chat"
    assert gate.decide(ctx([m("out", "Here you go.", 3, "Davis"), m("in", "thanks", 1)]), {"ack_only": True})["reason"] \
        == "acknowledgement_only"
    assert gate.decide(ctx([m("out", "Which dates?", 3, "Davis"), m("in", "ok", 1)]), {"ack_only": True})["respond"]
    last = m("in", "hi", 1)
    assert gate.decide(ctx([last], last_inbound_id=last["id"]), {})["reason"] == "already_handled"
    assert gate.decide(ctx([m("in", "hi", 1)], handoff=True), {})["reason"] == "handed_to_staff"


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
    assert s["handoff"] and s["stage"] == "payment"
    before = len(_davis(db))
    _guest(db, "hello?")
    assert _run(dc)["reason"] == "handed_to_staff" and len(_davis(db)) == before

    dc.post(f"/api/davis/sessions/{GUEST}/handoff", json={"active": False})
    assert not dc.get(f"/api/davis/sessions/{GUEST}").json()["session"]["handoff"]


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


def test_staff_reply_and_stale_messages_keep_davis_quiet(dc, db):
    _setup(dc, db)
    _guest(db, "Hi, any houses in Harare?", minutes_ago=90)
    assert _run(dc)["reason"] == "guest_message_too_old"
    db.execute("INSERT INTO messages (id, chat_id, sender, kind, text, created_at, direction, wa_status) "
               "VALUES (?,?,?,?,?,?, 'out', 'sent')",
               ("staff1", GUEST, "Front Desk", "text", "Hi Leon, which dates?",
                (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()))
    db.commit()
    _guest(db, f"{_human(CI)} to {_human(CO)}")
    assert _run(dc)["reason"] == "staff_replied_recently"
    assert _davis(db) == []


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
    assert s["handoff"] and s["handoff_reason"] == "complaint"


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

class _Block:
    def __init__(self, text):
        self.type, self.text = "text", text


class _Resp:
    def __init__(self, payload, stop="end_turn"):
        self.content = [_Block(json.dumps(payload))]
        self.stop_reason, self.model = stop, "claude-opus-5-5"
        self.usage = type("U", (), {"input_tokens": 900, "output_tokens": 60, "cache_read_input_tokens": 800,
                                    "cache_creation_input_tokens": 0})()


def _fake_claude(monkeypatch, payload):
    from app.davis import config, llm
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    calls = []

    class Messages:
        async def create(self, **kw):
            calls.append(kw)
            return _Resp(payload)

    def init(self, model, effort):
        import anthropic
        self._anthropic, self.model, self.effort = anthropic, model, effort
        self.client = type("C", (), {"beta": type("B", (), {"messages": Messages()})()})()
    monkeypatch.setattr(llm.AnthropicProvider, "__init__", init)
    return calls


def _model_out(reply, **kw):
    base = {"reply": reply, "full_name": "", "id_number": "", "city": "", "check_in": "", "check_out": "",
            "guests": "", "handoff": False, "handoff_reason": ""}
    base.update(kw)
    return base


def test_claude_request_shape_and_guarded_reply(dc, db, monkeypatch):
    _setup(dc, db)
    calls = _fake_claude(monkeypatch, _model_out("Lovely! Which dates would you like in Harare, and for how many?"))
    dc.put("/api/davis/settings", json={"provider": "anthropic"})
    _guest(db, "Hi, I need a place in Harare")
    r = _run(dc)
    kw = calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default"
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["output_config"]["effort"] == "low"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "temperature" not in kw and "thinking" not in kw
    assert "airbnb.com" not in kw["messages"][0]["content"]
    assert _davis(db)[-1]["text"].startswith("Lovely!") and r["plan"]["llm"]["cache_read_tokens"] == 800


def test_model_reply_with_invented_price_is_not_sent(dc, db, monkeypatch):
    _setup(dc, db)
    _fake_claude(monkeypatch, _model_out("Great news, all homes are just USD 20 a night! Which one?"))
    dc.put("/api/davis/settings", json={"provider": "anthropic"})
    _guest(db, f"Harare {_human(CI)} to {_human(CO)} for 4")
    r = _run(dc)
    assert r["plan"]["llm"]["rejected"].startswith("price_not_in_facts")
    assert "USD 20" not in _davis(db)[0]["text"] and "KCER" in _davis(db)[0]["text"]


def test_model_down_falls_back_and_breaker_opens(dc, db, monkeypatch):
    from app.davis import config, llm
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")

    class Boom:
        async def create(self, **kw):
            raise llm.LLMError("connection: refused")

    def init(self, model, effort):
        self.model, self.effort = model, effort
        self.client = type("C", (), {"beta": type("B", (), {"messages": Boom()})()})()
    monkeypatch.setattr(llm.AnthropicProvider, "__init__", init)
    dc.put("/api/davis/settings", json={"provider": "anthropic"})
    _setup(dc, db)
    for i in range(4):
        _guest(db, "Hi")
        _run(dc, again="true") if i else _run(dc)
        db.execute("DELETE FROM messages WHERE sender LIKE 'Davis%'")
        db.commit()
    st = dc.get("/api/davis/status").json()
    assert st["llm"]["breakers"]["anthropic"]["open"] and st["llm"]["stats"]["breaker_skips"] >= 1


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

    from app.davis import config
    monkeypatch.setattr(config, "ADMIN_TOKEN", "s3cret")
    assert dc.put("/api/davis/settings", json={"mode": "off"}).status_code == 401
    assert dc.put("/api/davis/settings", json={"mode": "off"}, headers={"X-Davis-Token": "s3cret"}).status_code == 200
    _guest(db, "Hi")
    r = dc.post(f"/api/davis/sessions/{GUEST}/run", headers={"X-Davis-Token": "s3cret"}).json()
    assert r["status"] == "drafted" and _davis(db) == []          # mode off: "run now" only drafts
