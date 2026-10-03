"""Invoice side: the options already sent to the guest come first, named as the
guest was told (KCER 101 …), and picking one fills the invoice."""
from datetime import datetime, timedelta, timezone

from app.wa_clean import chat_slug

GUEST = chat_slug("Leon")
OTHER = chat_slug("Someone Else")
_today = datetime.now(timezone.utc).date()
CI, CO = (_today + timedelta(days=5)).isoformat(), (_today + timedelta(days=7)).isoformat()


def _house(n, city="Harare", hood=None, usd=100.0):
    return {"url": f"https://www.airbnb.com/rooms/{n}", "title": f"Airbnb Title {n}", "location": city,
            "neighbourhood": hood, "capacity": "8 guests · 5 bedrooms", "check_in": CI, "check_out": CO,
            "nights": 2, "price_currency": "USD", "price_usd_per_night": usd, "price_raw": f"${usd * 2:.0f}"}


def _setup(client, db):
    for cid, name in ((GUEST, "Leon"), (OTHER, "Someone Else")):
        db.execute("INSERT INTO chats (id, name, avatar_emoji, created_at, wa_name) VALUES (?,?,?,?,?)",
                   (cid, name, "XX", "2026-10-01T00:00:00+00:00", name))
    db.commit()
    client.post("/api/houses/ingest", json={"listings": [
        _house(1, hood="Borrowdale"), _house(2, hood="Greendale", usd=128.5), _house(3, city="Bulawayo"),
        _house(4, hood="Avondale", usd=90.0)]})


def _urls(listings):
    return [h["url"][-1] for h in listings]


def test_sent_options_come_first_with_kcer_titles(client, db):
    _setup(client, db)
    ids = [f"https://www.airbnb.com/rooms/{n}" for n in (2, 4)]
    r = client.post("/api/houses/send", json={"chat_id": GUEST, "listing_ids": ids})
    assert r.status_code == 200

    plain = client.get("/api/houses/available", params={"location": "harare", "limit": 12}).json()
    assert all(h["title"].startswith("KCER 10") for h in plain)       # never the Airbnb title
    assert {h["airbnb_title"] for h in plain} >= {"Airbnb Title 2"}
    assert not any(h.get("sent_to_chat") for h in plain)

    mine = client.get("/api/houses/available", params={"location": "harare", "limit": 12, "chat_id": GUEST}).json()
    assert _urls(mine)[:2] == ["2", "4"] and [h["sent_to_chat"] for h in mine[:2]] == [True, True]
    assert mine[0]["title"] == "KCER 102 · Greendale, Harare" and mine[0]["ref_code"] == "KCER 102"
    assert sorted(_urls(mine)) == ["1", "2", "4"]                      # the rest follow, no repeats (Bulawayo excluded)
    assert (mine[0]["check_in"], mine[0]["check_out"], mine[0]["price_usd_per_night"]) == (CI, CO, 128.5)

    theirs = client.get("/api/houses/available", params={"location": "harare", "chat_id": OTHER}).json()
    assert not any(h.get("sent_to_chat") for h in theirs)              # another guest sees none as sent

    sent = client.get("/api/houses/sent", params={"chat_id": GUEST}).json()
    assert _urls(sent) == ["2", "4"] and sent[0]["option_no"] == 1


def test_invoice_for_a_kcer_name_is_linked_even_from_the_old_app(client, db):
    _setup(client, db)
    client.post("/api/houses/send", json={"chat_id": GUEST, "listing_ids": ["https://www.airbnb.com/rooms/2"]})
    # What the old app sends: the dropdown label as property name, a rate, no dates, no listing link.
    inv = client.post("/api/invoices", json={
        "guest_name": "Leon Pikiraih", "id_number": "63-2265159M22", "location": "harare",
        "property_name": "KCER 102 · Greendale, Harare", "rate": 128.5, "chat_id": GUEST, "guests": 2}).json()
    assert inv["property_name"] == "KCER 102"
    assert inv["listing_url"] == "https://www.airbnb.com/rooms/2"
    assert (inv["check_in"], inv["check_out"], inv["nights"]) == (CI, CO, 2)
    assert inv["total"] == 257.0
    assert inv["listing_title"] == "Airbnb Title 2"                      # kept for staff, not printed

    # Name that matches nothing stays as typed, with no listing link.
    other = client.post("/api/invoices", json={"guest_name": "A", "id_number": "1", "location": "harare",
                                               "property_name": "Some Flat", "rate": 50, "nights": 1}).json()
    assert other["property_name"] == "Some Flat" and other["listing_url"] is None


def test_invoice_from_the_new_app_keeps_the_sent_offer(client, db):
    _setup(client, db)
    client.post("/api/houses/send", json={"chat_id": GUEST, "listing_ids": ["https://www.airbnb.com/rooms/1"]})
    mine = client.get("/api/houses/available", params={"location": "harare", "chat_id": GUEST}).json()[0]
    inv = client.post("/api/invoices", json={
        "guest_name": "Leon", "id_number": "1", "location": "harare", "property_name": mine["title"],
        "listing_url": mine["url"], "listing_offer_id": mine["offer_id"], "check_in": mine["check_in"],
        "check_out": mine["check_out"], "rate": mine["price_usd_per_night"], "chat_id": GUEST}).json()
    assert inv["property_name"] == "KCER 101" and inv["listing_offer_id"] == mine["offer_id"]
