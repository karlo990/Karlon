"""Server side of the per-chat snapshot: bad rows in, one clean ordered chat out."""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pc-bridge"))
from datetime import datetime, timedelta, timezone  # noqa: E402

from app.wa_clean import chat_slug  # noqa: E402

KARL = chat_slug("Karl")
TZ = timezone(timedelta(hours=2))


def _insert_chat(db, cid, name):
    db.execute("INSERT INTO chats (id, name, avatar_emoji, created_at, wa_name) VALUES (?,?,?,?,?)",
               (cid, name, "1U", "2026-09-01T00:00:00+00:00", name))


def _insert_msg(db, mid, cid, text, created, key=None, direction="in", wa_status=None,
                kind="text", sender="Karl", media_url=None):
    db.execute("INSERT INTO messages (id, chat_id, sender, kind, text, created_at, external_key, "
               "direction, wa_status, media_url) VALUES (?,?,?,?,?,?,?,?,?,?)",
               (mid, cid, sender, kind, text, created, key, direction, wa_status, media_url))


def _msg(seq, text, created, key, direction="in", kind="text", wa_id=None):
    return {"seq": seq, "external_key": key, "wa_id": wa_id, "direction": direction,
            "sender": "You (WhatsApp)" if direction == "out" else "Karl", "kind": kind,
            "text": text, "created_at": created, "time_source": "metadata"}


def _snap(messages, name="Karl", window_start=None, window_end="2026-09-30T05:30:00.000000+00:00"):
    return {"schema": "karlon.chat_snapshot/1",
            "chat": {"id": chat_slug(name), "name": name, "wa_name": name, "avatar_emoji": "KA"},
            "scanned_at": window_end,
            "window_start": window_start or (messages[0]["created_at"] if messages else None),
            "window_end": window_end, "reached_top": False, "messages": messages}


IMG_KEY = hashlib.sha1(b"img|false_263@c.us_IMG").hexdigest()


def _seed_polluted(db):
    """What older bridges left behind for Karl (see the bug report screenshots)."""
    alias1 = chat_slug("1 unread message\nKarl")
    alias2 = chat_slug("2 unread messages\nKarl")
    _insert_chat(db, alias1, "1 unread message\nKarl")
    _insert_chat(db, alias2, "2 unread messages\nKarl")
    # naive local time (older bridge) — sorted 2 h off against UTC rows
    _insert_msg(db, "old-1", alias1, "No pressure I understand school boy", "2026-09-29T20:29:00", key="k-nopressure")
    # the same message again under the other alias (duplicate)
    _insert_msg(db, "old-1b", alias2, "No pressure I understand school boy", "2026-09-29T20:29:00", key="k-nopressure")
    # encryption notice, stamped with the import time
    _insert_msg(db, "junk-e2e", alias1, "Messages and calls are end-to-end encrypted. Click to learn more",
                "2026-09-30T02:38:10.123456+00:00", key="k-e2e", sender="1 unread message\nKarl")
    # row-path duplicate of 'No pressure' (different key) stamped at import time
    _insert_msg(db, "dup-row", alias1, "No pressure I understand school boy",
                "2026-09-30T02:38:11.000000+00:00", key="k-row-dup", sender="1 unread message\nKarl")
    # photo with the time glued into its caption, stamped with the import time
    _insert_msg(db, "img-1", alias1, "Still not being able to do anything can you fix it or.......\n20:16",
                "2026-09-30T02:38:12.000000+00:00", key=IMG_KEY, kind="image", media_url="/static/media/x.jpg")
    # app-sent message (already delivered) and its WhatsApp echo will arrive in the snapshot
    _insert_msg(db, "app-sent", alias1, "Forgive me", "2026-09-29T18:18:40.000000+00:00",
                key=None, direction="out", wa_status="sent", sender="Front Desk")
    # app-composed message still waiting for WhatsApp: must never be touched
    _insert_msg(db, "app-pending", alias1, "Your invoice is ready", "2026-09-30T05:00:00.000000+00:00",
                direction="out", wa_status="pending", sender="Front Desk")
    # an invoice attached to the alias chat
    db.execute("INSERT INTO invoices (id, chat_id, guest_name, id_number, location, property_name, "
               "created_at, updated_at) VALUES ('inv1', ?, 'Karl', 'X1', 'harare', 'R41', 'n', 'n')", (alias1,))
    db.commit()
    return alias1, alias2


SNAPSHOT = [
    _msg(0, "Still not being able to do anything can you fix it or.......",
         "2026-09-29T18:16:00.000000+00:00", IMG_KEY, kind="image", wa_id="false_263@c.us_IMG"),
    _msg(1, "Forgive me", "2026-09-29T18:19:00.000000+00:00", "k-forgive", direction="out", wa_id="true_M3"),
    _msg(2, "No pressure I understand school boy", "2026-09-29T18:29:00.000000+00:00", "k-nopressure", wa_id="false_M4"),
    _msg(3, "So in order for me to see messages at Karlon", "2026-09-29T18:29:00.001000+00:00", "k-soinorder", wa_id="false_M5"),
    _msg(4, "Gd mng", "2026-09-30T04:38:00.000000+00:00", "k-gdmng", direction="out", wa_id="true_M10"),
    _msg(5, "new photo", "2026-09-30T04:39:00.000000+00:00", "k-newimg", kind="image", wa_id="false_IMG2"),
]


def test_sync_cleans_the_whole_chat_and_orders_it(client, db):
    alias1, alias2 = _seed_polluted(db)
    r = client.post(f"/api/chats/{KARL}/sync", json=_snap(SNAPSHOT))
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["aliases_merged"] == 2
    assert rep["missing_images"] == ["k-newimg"]        # bridge uploads only this one
    assert rep["linked_app_messages"] == 1               # WhatsApp echo of "Forgive me" not duplicated
    assert rep["pruned"] == 2                            # encryption notice + row-path duplicate

    chats = client.get("/api/chats").json()
    assert [c["name"] for c in chats] == ["Karl"]        # one chat, real name
    assert chats[0]["id"] == KARL and chats[0]["wa_name"] == "Karl"

    msgs = client.get(f"/api/chats/{KARL}/messages").json()
    assert [(m["text"], m["direction"]) for m in msgs] == [
        ("Still not being able to do anything can you fix it or.......", "in"),
        ("Forgive me", "out"),                           # the app's own row, linked
        ("No pressure I understand school boy", "in"),
        ("So in order for me to see messages at Karlon", "in"),
        ("Gd mng", "out"),
        ("Your invoice is ready", "out"),                # pending app message untouched
    ]
    ts = [m["created_at"] for m in msgs]
    assert ts == sorted(ts)
    assert all(t.endswith("+02:00") for t in ts)         # served in Harare time ...
    stored = [r[0] for r in db.execute(
        f"SELECT created_at FROM messages WHERE chat_id='{KARL}' ORDER BY created_at, rowid")]
    assert all(t.endswith("+00:00") for t in stored)     # ... stored in UTC
    img = msgs[0]
    assert img["id"] == "img-1" and img["created_at"] == "2026-09-29T20:16:00.000000+02:00"
    pending = next(m for m in msgs if m["id"] == "app-pending")
    assert pending["wa_status"] == "pending" and pending["sender"] == "Front Desk"
    assert db.execute("SELECT chat_id FROM invoices WHERE id='inv1'").fetchone()[0] == KARL
    assert db.execute("SELECT COUNT(*) FROM chats WHERE id IN (?,?)", (alias1, alias2)).fetchone()[0] == 0


def test_sync_is_idempotent(client, db):
    _seed_polluted(db)
    client.post(f"/api/chats/{KARL}/sync", json=_snap(SNAPSHOT))
    before = client.get(f"/api/chats/{KARL}/messages").json()
    rep = client.post(f"/api/chats/{KARL}/sync", json=_snap(SNAPSHOT)).json()
    assert (rep["inserted"], rep["updated"], rep["pruned"], rep["aliases_merged"]) == (0, 0, 0, 0)
    assert client.get(f"/api/chats/{KARL}/messages").json() == before


def test_prune_safety_valve(client, db):
    _insert_chat(db, KARL, "Karl")
    for i in range(100):
        _insert_msg(db, f"m{i}", KARL, f"msg {i}", f"2026-09-29T10:{i % 60:02d}:00.000000+00:00", key=f"k{i}")
    db.commit()
    one = [_msg(0, "msg 0", "2026-09-29T10:00:00.000000+00:00", "k0")]
    rep = client.post(f"/api/chats/{KARL}/sync", json=_snap(one)).json()
    assert rep["pruned"] == 0 and "would remove 99 of 100" in rep["prune_skipped"]
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 100
    rep = client.post(f"/api/chats/{KARL}/sync?force=true", json=_snap(one)).json()
    assert rep["pruned"] == 99


def test_rows_outside_the_window_are_kept(client, db):
    _insert_chat(db, KARL, "Karl")
    _insert_msg(db, "older", KARL, "from last month", "2026-08-01T10:00:00.000000+00:00", key="k-old")
    db.commit()
    rep = client.post(f"/api/chats/{KARL}/sync", json=_snap(SNAPSHOT[1:5])).json()
    assert rep["pruned"] == 0
    assert "from last month" in [m["text"] for m in client.get(f"/api/chats/{KARL}/messages").json()]


def test_server_rejects_bad_data_even_from_an_old_client(client):
    bad = [_msg(0, "Messages and calls are end-to-end encrypted.", "2026-09-29T18:00:00Z", "a"),
           _msg(1, "20:16", "2026-09-29T18:01:00Z", "b"),
           _msg(2, "real", "not-a-date", "c"),
           _msg(3, "real", "2026-09-29T18:02:00Z", "d")]
    rep = client.post(f"/api/chats/{KARL}/sync", json=_snap(bad)).json()
    assert rep["dropped_invalid"] == 3 and rep["inserted"] == 1


def test_sync_refuses_a_polluted_or_mismatched_identity(client):
    r = client.post(f"/api/chats/{chat_slug('1 unread message')}/sync",
                    json=_snap(SNAPSHOT[1:2], name="1 unread message"))
    assert r.status_code == 422
    body = _snap(SNAPSHOT[1:2])
    r = client.post(f"/api/chats/{chat_slug('Someone else')}/sync", json=body)
    assert r.status_code == 422


def test_repair_names_dry_run_then_apply(client, db):
    alias1, alias2 = _seed_polluted(db)
    _insert_chat(db, chat_slug("3 unread messages"), "3 unread messages")
    db.commit()
    dry = client.post("/api/chats/repair-names").json()
    assert not dry["applied"]
    assert {(p["from"], p["to"]) for p in dry["merges"]} == {(alias1, KARL), (alias2, KARL)}
    assert [u["name"] for u in dry["unrecoverable"]] == ["3 unread messages"]
    assert db.execute("SELECT COUNT(*) FROM chats").fetchone()[0] == 3

    done = client.post("/api/chats/repair-names?apply=true&delete_unrecoverable=true").json()
    assert done["applied"] and done["unrecoverable_deleted"] == [chat_slug("3 unread messages")]
    assert [c["name"] for c in client.get("/api/chats").json()] == ["Karl"]
    texts = [m["text"] for m in client.get(f"/api/chats/{KARL}/messages").json()]
    assert texts.count("No pressure I understand school boy") == 2   # exact dup (same key) dropped; row-path dup left for /sync


def test_bridge_snapshot_round_trip(client):
    """The snapshot the bridge actually builds is accepted as-is."""
    import wa_bridge
    units = [
        {"type": "divider", "text": "YESTERDAY", "y": 0},
        {"type": "msg", "dataId": "false_1", "pre": "[20:29, 29/09/2026] Karl: ", "text": "hello",
         "time": "", "direction": "in", "y": 1},
        {"type": "msg", "dataId": "true_2", "pre": "", "text": "hi Karl", "time": "20:30",
         "direction": "out", "y": 2},
    ]
    snap = wa_bridge.build_chat_snapshot(units, "Karl", now_local=datetime(2026, 9, 30, 7, 0), tz=TZ)
    body = {k: v for k, v in snap.items() if k != "rejected"}
    rep = client.post(f"/api/chats/{snap['chat']['id']}/sync", json=body).json()
    assert rep["inserted"] == 2
    msgs = client.get(f"/api/chats/{KARL}/messages").json()
    assert [(m["text"], m["direction"], m["created_at"]) for m in msgs] == [
        ("hello", "in", "2026-09-29T20:29:00.000000+02:00"),     # WhatsApp showed 20:29
        ("hi Karl", "out", "2026-09-29T20:30:00.000000+02:00"),
    ]


def test_legacy_import_normalises_time_and_drops_notices(client, db):
    _insert_chat(db, KARL, "Karl")
    db.commit()
    rep = client.post(f"/api/chats/{KARL}/import", json={"messages": [
        {"sender": "Karl", "text": "Messages and calls are end-to-end encrypted.", "external_key": "a"},
        {"sender": "Karl", "text": "hello", "created_at": "2026-09-29T20:29:00", "external_key": "b"},
    ]}).json()
    assert rep["imported"] == 1
    assert client.get(f"/api/chats/{KARL}/messages").json()[0]["created_at"] == "2026-09-29T20:29:00.000000+02:00"
    assert db.execute("SELECT created_at FROM messages").fetchone()[0] == "2026-09-29T18:29:00.000000+00:00"


def test_upsert_chat_stores_the_clean_name(client):
    r = client.post("/api/chats", json={"id": "wa-x", "name": "1 unread message\nKarl",
                                        "wa_name": "1 unread message\nKarl"})
    assert r.json()["name"] == "Karl" and r.json()["wa_name"] == "Karl"


def test_wa_clean_copies_are_identical():
    root = Path(__file__).resolve().parents[2]
    assert (root / "server/app/wa_clean.py").read_bytes() == (root / "pc-bridge/wa_clean.py").read_bytes()


def test_chat_list_serves_local_time_and_web_page_handles_refresh(client, db):
    _insert_chat(db, KARL, "Karl")
    _insert_msg(db, "m1", KARL, "hi", "2026-09-30T10:13:00.000000+00:00", key="k1")
    db.commit()
    assert client.get("/api/chats").json()[0]["last_at"] == "2026-09-30T12:13:00.000000+02:00"
    page = (Path(__file__).resolve().parents[1] / "static/index.html").read_text(encoding="utf-8")
    assert "payload.type === 'refresh'" in page


def test_house_send_is_details_text_then_plain_photos(client, db):
    _insert_chat(db, KARL, "Karl")
    db.commit()
    listing = {"url": "https://www.airbnb.com/rooms/1731941150603946314", "title": "8 @Metcalf One",
               "location": "Harare", "check_in": "2026-10-03", "check_out": "2026-10-25", "nights": 22,
               "price_raw": "$3,172 for 22 nights", "price_currency": "USD", "price_usd_per_night": 144.18,
               "images": ["https://a0.muscache.com/im/pictures/1.jpg", "https://a0.muscache.com/im/pictures/2.jpg"],
               "neighbourhood": "Greendale", "capacity": "6 guests · 3 bedrooms · 3 beds · 2.5 baths",
               "rating": "4.67", "reviews_count": "3"}
    assert client.post("/api/houses/ingest", json={"listings": [listing]}).status_code == 200
    offers = client.get("/api/houses/available", params={"location": "harare", "check_in": "2026-10-03",
                                                          "check_out": "2026-10-25"}).json()
    assert offers, offers
    r = client.post("/api/houses/send", json={"chat_id": KARL, "listing_ids": [listing["url"]],
                                              "offer_ids": [offers[0].get("offer_id")]})
    assert r.status_code == 200, r.text
    msgs = [m for m in r.json()["messages"]]
    assert [m["kind"] for m in msgs] == ["text", "image", "image"]
    text = msgs[0]["text"]
    assert text.startswith("This is what I have found for you:")
    for part in ("📍 Greendale, Harare", "👥 6 guests · 3 bedrooms · 3 beds · 2.5 baths", "⭐ 4.67 (3 reviews)",
                 "📅 Free 03 Oct 2026 → 25 Oct 2026 (22 nights)", "💵 USD 144.18 per night · USD 3,171.96 total"):
        assert part in text, (part, text)
    assert "Managed by" not in text and "airbnb.com" not in text
    assert all(not m["text"] for m in msgs[1:])                         # photos without captions
    order = [r[0] for r in db.execute(
        "SELECT kind FROM messages WHERE chat_id=? AND wa_status='pending' ORDER BY created_at, rowid", (KARL,))]
    assert order == ["text", "image", "image"]                          # outbox delivers in this order
