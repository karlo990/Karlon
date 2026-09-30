import queue

import wa_bridge as w


def _sidebar(rows):
    items = "".join(
        f'<div data-testid="cell-frame-container" role="listitem" style="height:40px">'
        f'<span title="{n}" dir="auto">{n}</span>'
        f'<span data-testid="cell-frame-secondary">{p}</span><span class="t">{t}</span></div>'
        for n, p, t in rows
    )
    return f'<div id="pane-side" style="height:400px;overflow:auto">{items}</div>'


def test_chat_rows_fingerprint_changes_with_activity_and_skip_names(page):
    page.set_content(_sidebar([("Thabo", "ok", "10:01"), ("Nomsa", "hey", "09:00")]))
    a = {r["name"]: r for r in w.get_chat_rows(page)}
    assert set(a) == {"Thabo", "Nomsa"}
    assert all(len(r["fingerprint"]) == 40 for r in a.values())

    page.set_content(_sidebar([("Thabo", "ok", "10:01"), ("Nomsa", "hey", "09:05")]))
    b = {r["name"]: r for r in w.get_chat_rows(page)}
    assert b["Thabo"]["fingerprint"] == a["Thabo"]["fingerprint"]     # unchanged chat
    assert b["Nomsa"]["fingerprint"] != a["Nomsa"]["fingerprint"]     # new activity

    only = w.get_chat_rows(page, skip_names={"Thabo"})
    assert [r["name"] for r in only] == ["Nomsa"]


def _fake_pass(monkeypatch, chats, opened, ok=lambda: True, full_every=3):
    monkeypatch.setattr(w, "get_all_chat_rows", lambda page: [dict(c) for c in chats])
    monkeypatch.setattr(w, "get_recent_chat_rows", lambda page, limit: [dict(c) for c in chats][:limit])
    monkeypatch.setattr(w, "sync_chat",
                        lambda page, name, *a, **k: opened.append(name) or ({} if ok() else None))
    monkeypatch.setattr(w, "WA_FULL_SWEEP_EVERY", full_every)
    monkeypatch.setattr(w, "_chat_fingerprints", {})
    monkeypatch.setattr(w, "_sync_pass", 0)


def test_sync_once_skips_unchanged_chats_but_full_sweeps_periodically(monkeypatch):
    chats = [
        {"name": "Thabo", "preview": "", "unread": 0, "pic_src": None, "fingerprint": "fp-thabo-1"},
        {"name": "Nomsa", "preview": "", "unread": 0, "pic_src": None, "fingerprint": "fp-nomsa-1"},
    ]
    opened = []
    _fake_pass(monkeypatch, chats, opened)
    w.sync_once(None, sync_all=False, fetch_pics=False)         # pass 1: full sweep
    assert opened == ["Thabo", "Nomsa"]
    opened.clear()
    w.sync_once(None, sync_all=False, fetch_pics=False)         # pass 2: nothing changed
    assert opened == []
    chats[1]["fingerprint"] = "fp-nomsa-2"                      # Nomsa gets activity
    w.sync_once(None, sync_all=False, fetch_pics=False)         # pass 3: only Nomsa
    assert opened == ["Nomsa"]
    opened.clear()
    w.sync_once(None, sync_all=False, fetch_pics=False)         # pass 4: periodic full sweep
    assert opened == ["Thabo", "Nomsa"]


def test_default_pass_reads_only_the_most_recent_chats(monkeypatch):
    chats = [{"name": f"C{i}", "preview": "", "unread": 0, "pic_src": None, "fingerprint": str(i)}
             for i in range(20)]
    opened = []
    _fake_pass(monkeypatch, chats, opened)
    monkeypatch.setattr(w, "WA_SYNC_LAST_N_CHATS", 15)
    w.sync_once(None, sync_all=False, fetch_pics=False)
    assert opened == [f"C{i}" for i in range(15)]
    opened.clear()
    w.sync_once(None, sync_all=False, fetch_pics=False, only_chat="1 unread message\nKarl")
    assert opened == ["Karl"]                                   # --chat is cleaned too


def test_failed_chat_is_not_fingerprinted(monkeypatch):
    chats = [{"name": "Thabo", "preview": "", "unread": 0, "pic_src": None, "fingerprint": "fp"}]
    state = {"ok": False}
    opened = []
    _fake_pass(monkeypatch, chats, opened, ok=lambda: state["ok"], full_every=100)
    monkeypatch.setattr(w, "_chat_failures", {})
    w.sync_once(None, False, False)                             # sync fails
    state["ok"] = True
    w.sync_once(None, False, False)                             # backing off: not retried yet
    assert opened == ["Thabo"]
    w._chat_failures["Thabo"] = (1, 0.0)                        # back-off expired
    w.sync_once(None, False, False)                             # retried (was never fingerprinted)
    assert opened == ["Thabo", "Thabo"]
    assert "Thabo" not in w._chat_failures


def test_first_read_is_full_then_recent(monkeypatch):
    chats = [{"name": "Thabo", "preview": "", "unread": 0, "pic_src": None, "fingerprint": "a"}]
    modes = []
    _fake_pass(monkeypatch, chats, [], full_every=100)
    monkeypatch.setattr(w, "_synced_once", set())
    monkeypatch.setattr(w, "_chat_failures", {})
    monkeypatch.setattr(w, "sync_chat", lambda page, name, *a, mode="full", **k: modes.append(mode) or {})
    w.sync_once(None, False, False)
    chats[0]["fingerprint"] = "b"                                # new message arrives
    w.sync_once(None, False, False)
    assert modes == ["full", "recent"]


def test_urgent_reply_is_delivered_between_chats_even_if_normal_queue_is_empty(monkeypatch):
    chats = [{"name": n, "preview": "", "unread": 0, "pic_src": None, "fingerprint": n}
             for n in ("A", "B")]
    delivered = []
    _fake_pass(monkeypatch, chats, [])
    monkeypatch.setattr(w, "process_outbox", lambda page: delivered.append("urgent-first"))
    monkeypatch.setattr(w, "_outbox", queue.Queue())
    monkeypatch.setattr(w, "_urgent_outbox", queue.Queue())
    w._urgent_outbox.put({"id": "m1"})
    assert w.outbox_pending()
    w.sync_once(None, False, False)
    assert delivered, "urgent item never triggered process_outbox during the sync pass"


def test_failed_urgent_send_is_requeued_as_urgent(monkeypatch):
    acks = []
    monkeypatch.setattr(w, "_outbox", queue.Queue())
    monkeypatch.setattr(w, "_urgent_outbox", queue.Queue())
    monkeypatch.setattr(w, "open_chat_row", lambda page, name: True)
    monkeypatch.setattr(w, "get_open_chat_title", lambda page: "Thabo")
    monkeypatch.setattr(w, "dom_send_message", lambda page, text: False)
    monkeypatch.setattr(w, "_ack_wa_message", lambda *a, **k: acks.append(a))
    import threading
    item = {"id": "u1", "chat_id": "c", "wa_name": "Thabo", "text": "your booking is confirmed", "_retries": 0}
    w._process_items(None, [item], "urgent", threading.Lock(), set())
    assert w._urgent_outbox.qsize() == 1 and w._outbox.qsize() == 0
    assert item["_retries"] == 1 and not acks


def test_ack_is_retried_then_gives_up(monkeypatch):
    calls = []

    class R:
        def raise_for_status(self):
            if len(calls) < 3:
                raise RuntimeError("boom")

    class S:
        def patch(self, *a, **k):
            calls.append(1)
            return R()

    monkeypatch.setattr(w, "get_session", lambda: S())
    monkeypatch.setattr(w.time, "sleep", lambda s: None)
    w._ack_wa_message("c", "m", "sent")
    assert len(calls) == 3
