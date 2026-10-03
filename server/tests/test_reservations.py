"""Reserve flow: app -> server queue -> PC (one at a time) -> WhatsApp message."""
from datetime import datetime, timedelta, timezone

from app.wa_clean import chat_slug

KARL = chat_slug("Karl")
URL = "https://www.airbnb.com/rooms/1731941150603946314"
_today = datetime.now(timezone.utc).date()
CI, CO = (_today + timedelta(days=2)).isoformat(), (_today + timedelta(days=5)).isoformat()


def _setup(client, db):
    db.execute("INSERT INTO chats (id, name, avatar_emoji, created_at, wa_name) VALUES (?,?,?,?,?)",
               (KARL, "Karl", "KA", "2026-10-01T00:00:00+00:00", "Karl"))
    db.commit()
    client.post("/api/houses/ingest", json={"listings": [{
        "url": URL, "title": "8 @Metcalf One", "location": "Harare", "check_in": CI, "check_out": CO,
        "nights": 3, "price_raw": "$422 for 3 nights", "price_currency": "USD", "price_usd_per_night": 140.67}]})
    return client.get("/api/houses/available", params={"location": "harare", "check_in": CI,
                                                       "check_out": CO}).json()[0]


def _reserve(client, listing, **kw):
    body = {"listing_id": listing["url"], "offer_id": listing.get("offer_id"), "chat_id": KARL, "guests": 3}
    body.update(kw)
    return client.post("/api/reservations", json=body)


def test_create_claim_complete_sends_whatsapp(client, db):
    listing = _setup(client, db)
    r = _reserve(client, listing)
    assert r.status_code == 200, r.text
    res = r.json()
    assert (res["status"], res["airbnb_id"], res["ref_code"], res["nights"], res["guests"]) == \
           ("pending", "1731941150603946314", "KCER 101", 3, 3)
    assert abs(res["expected_total_usd"] - 422.01) < 0.01

    jobs = client.get("/api/reservations/pending").json()
    assert [j["id"] for j in jobs] == [res["id"]] and jobs[0]["status"] == "in_progress"
    assert client.get("/api/reservations/pending").json() == []          # nothing else while one runs

    done = client.post(f"/api/reservations/{res['id']}/complete",
                       json={"status": "requested", "total_usd": 421.95,
                             "trip_url": "https://www.airbnb.com/trips/v1/1"}).json()
    assert done["status"] == "requested" and done["total_usd"] == 421.95
    out = [dict(m) for m in db.execute(
        "SELECT text, direction, wa_status FROM messages WHERE chat_id=?", (KARL,))]
    assert len(out) == 1 and out[0]["direction"] == "out" and out[0]["wa_status"] == "pending"
    assert "Your reservation has been made" in out[0]["text"]
    assert "KCER 101" in out[0]["text"]
    assert "waiting for the host to share the live location" in out[0]["text"]
    assert "Metcalf" not in out[0]["text"]                               # never the Airbnb title


def test_duplicate_request_for_same_dates_is_refused(client, db):
    listing = _setup(client, db)
    assert _reserve(client, listing).status_code == 200
    assert _reserve(client, listing).status_code == 409


def test_failed_or_dry_run_sends_nothing_and_can_be_retried(client, db):
    listing = _setup(client, db)
    for status in ("failed", "dry_run"):
        rid = _reserve(client, listing).json()["id"]
        client.get("/api/reservations/pending")
        client.post(f"/api/reservations/{rid}/complete", json={"status": status, "error_message": "x"})
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert _reserve(client, listing).status_code == 200


def test_crashed_booking_becomes_unknown_never_pending(client, db):
    listing = _setup(client, db)
    rid = _reserve(client, listing).json()["id"]
    client.get("/api/reservations/pending")
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db.execute("UPDATE reservations SET started_at=? WHERE id=?", (old, rid))
    db.commit()
    assert client.get("/api/reservations/pending").json() == []          # not handed out again
    assert client.get(f"/api/reservations/{rid}").json()["status"] == "unknown"
    assert _reserve(client, listing).status_code == 409                 # and blocks a second booking


def test_only_pending_can_be_cancelled_and_dates_are_required(client, db):
    listing = _setup(client, db)
    rid = _reserve(client, listing).json()["id"]
    assert client.post(f"/api/reservations/{rid}/cancel").json()["status"] == "cancelled"
    rid2 = _reserve(client, listing).json()["id"]
    client.get("/api/reservations/pending")
    assert client.post(f"/api/reservations/{rid2}/cancel").status_code == 409
    client.post("/api/houses/ingest", json={"listings": [{"url": "https://www.airbnb.com/rooms/9",
                                                          "location": "Harare"}]})
    r = client.post("/api/reservations", json={"listing_id": "https://www.airbnb.com/rooms/9", "guests": 1})
    assert r.status_code == 422


def test_complete_only_from_in_progress(client, db):
    listing = _setup(client, db)
    rid = _reserve(client, listing).json()["id"]
    r = client.post(f"/api/reservations/{rid}/complete", json={"status": "requested"})
    assert r.status_code == 409                                          # never claimed by the PC
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
