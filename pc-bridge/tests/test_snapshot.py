"""The per-chat snapshot, on a fake chat modelled on the Karl conversation in
the bug report screenshots (encryption notice imported as a message, a photo
with "20:16" glued into its caption and stamped with the import time, unread
divider, messages out of order).

The DOM is a model of WhatsApp Web's markup: it proves the logic, not that
today's live markup still matches (check that with --chat NAME --dry-run).
"""
import hashlib
from datetime import datetime, timedelta, timezone

import wa_bridge as w

TZ = timezone(timedelta(hours=2))                   # Harare
NOW = datetime(2026, 9, 30, 7, 30)                  # local, the screenshots' morning
IMG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def _row(inner, order, side="left", data_id=None):
    justify = {"left": "flex-start", "right": "flex-end", "center": "center"}[side]
    did = f' data-id="{data_id}"' if data_id else ""
    return (f'<div role="row" style="order:{order};display:flex;justify-content:{justify};margin:4px">'
            f'<div{did} style="max-width:60%">{inner}</div></div>')


def _text(pre, text, time=None):
    meta = f'<span data-testid="msg-meta"><span>{time}</span></span>' if time else ""
    pre_attr = f' data-pre-plain-text="{pre}"' if pre else ""
    return f'<div class="copyable-text"{pre_attr}><span class="selectable-text">{text}</span></div>{meta}'


# (visual order, html). DOM order is deliberately NOT visual order.
ROWS = [
    (5, _row('<span data-icon="tail-in"></span>'
             + _text("[20:29, 29/09/2026] Karl: ", "So in order for me to see messages at Karlon"),
             5, "left", "false_263@c.us_M5")),
    (1, _row("<span>YESTERDAY</span>", 1)),
    (3, _row('<span data-icon="tail-out"></span>' + _text("[20:19, 29/09/2026] Karlo: ", "Forgive me"),
             3, "right", "true_263@c.us_M3")),
    (2, _row(f'<img src="{IMG}" style="width:200px;height:150px">'
             '<span class="selectable-text">Still not being able to do anything can you fix it or.......'
             '<br>20:16</span>', 2, "left", "false_263@c.us_IMG")),
    (4, _row('<span data-icon="tail-in"></span>'
             + _text("[20:29, 29/09/2026] Karl: ", "No pressure I understand school boy"),
             4, "left", "false_263@c.us_M4")),
    (6, _row("<span>1 unread message</span>", 6)),
    (7, _row("<span>TODAY</span>", 7)),
    (8, _row('<span dir="auto">Messages and calls are end-to-end encrypted. Only people in this chat '
             'can read, listen to, or share them. Click to learn more</span>', 8, "center", "false_263@c.us_E2E")),
    (9, _row('<span dir="auto">Karl changed their profile photo</span>', 9, "center", "x_SYS")),
    (10, _row('<span data-icon="tail-out"></span><span class="selectable-text">Gd mng</span>'
              '<span data-testid="msg-meta"><span>06:38</span></span>', 10, "right", "true_263@c.us_M10")),
    (11, _row('<span data-icon="audio-play"></span><span data-testid="msg-meta"><span>06:40</span></span>',
              11, "left", "false_263@c.us_VOICE")),
]


def _load(page):
    html = "".join(h for _, h in ROWS)
    page.set_content(
        '<div id="main" style="width:860px"><div data-testid="conversation-panel-messages" '
        'style="width:860px;height:640px;overflow:auto;display:flex;flex-direction:column">'
        + html + "</div></div>")


def _snapshot(page):
    _load(page)
    units = w.walk_chat(page)
    return w.build_chat_snapshot(units, "Karl", now_local=NOW, tz=TZ)


def test_walk_returns_rows_in_screen_order_not_dom_order(page):
    _load(page)
    units = w.walk_chat(page)
    ys = [u["y"] for u in units]
    assert ys == sorted(ys)
    assert units[0] == {**units[0], "type": "divider", "text": "YESTERDAY"}


def test_snapshot_keeps_only_real_messages_in_whatsapp_order(page):
    snap = _snapshot(page)
    msgs = snap["messages"]
    assert [m["text"] for m in msgs] == [
        "Still not being able to do anything can you fix it or.......",   # glued "20:16" removed
        "Forgive me",
        "No pressure I understand school boy",
        "So in order for me to see messages at Karlon",
        "Gd mng",
    ]
    assert [m["direction"] for m in msgs] == ["in", "out", "in", "in", "out"]
    assert [m["kind"] for m in msgs] == ["image", "text", "text", "text", "text"]
    assert [m["seq"] for m in msgs] == list(range(5))
    # bad data is rejected with a reason, never silently imported
    assert sorted(r["reason"] for r in snap["rejected"]) == [
        "no_message_metadata", "system_notice", "unsupported_media"]
    assert snap["stats"]["unread_dividers"] == 1


def test_snapshot_times_are_utc_whatsapp_times_and_sort_in_screen_order(page):
    msgs = _snapshot(page)["messages"]
    assert [m["created_at"] for m in msgs] == [
        "2026-09-29T18:16:00.000000+00:00",   # photo: 20:16 yesterday, NOT the import time
        "2026-09-29T18:19:00.000000+00:00",
        "2026-09-29T18:29:00.000000+00:00",   # same minute ...
        "2026-09-29T18:29:00.001000+00:00",   # ... keeps screen order (+1 ms)
        "2026-09-30T04:38:00.000000+00:00",   # bubble time 06:38 under TODAY
    ]
    assert [m["time_source"] for m in msgs] == [
        "divider+bubble", "metadata", "metadata", "metadata", "divider+bubble"]
    assert [m["created_at"] for m in msgs] == sorted(m["created_at"] for m in msgs)


def test_snapshot_keys_match_what_earlier_builds_stored(page):
    msgs = {m["text"]: m for m in _snapshot(page)["messages"]}
    img = msgs["Still not being able to do anything can you fix it or......."]
    assert img["external_key"] == hashlib.sha1(b"img|false_263@c.us_IMG").hexdigest()
    fm = msgs["Forgive me"]
    assert fm["external_key"] == hashlib.sha1(
        "[20:19, 29/09/2026] Karlo: |Forgive me".encode()).hexdigest()
    assert fm["wa_id"] == "true_263@c.us_M3"


def test_snapshot_chat_block_uses_the_clean_name(page):
    snap = _snapshot(page)
    assert snap["schema"] == "karlon.chat_snapshot/1"
    assert snap["chat"] == {"id": w.chat_slug("Karl"), "name": "Karl", "wa_name": "Karl", "avatar_emoji": "KA"}
    assert snap["window_start"] == snap["messages"][0]["created_at"]


def test_out_of_order_times_are_clamped_to_screen_order():
    units = [
        {"type": "msg", "dataId": "a", "pre": "[10:05, 01/09/2026] X: ", "text": "first", "time": "", "direction": "in"},
        {"type": "msg", "dataId": "b", "pre": "[10:01, 01/09/2026] X: ", "text": "second", "time": "", "direction": "in"},
    ]
    snap = w.build_chat_snapshot(units, "X", now_local=NOW, tz=TZ)
    assert [m["text"] for m in snap["messages"]] == ["first", "second"]
    assert snap["messages"][0]["created_at"] < snap["messages"][1]["created_at"]
    assert snap["stats"]["clamped"] == 1


def test_leading_messages_without_a_date_inherit_from_the_next_one():
    units = [
        {"type": "msg", "dataId": "a", "pre": "", "text": "hi", "time": "09:00", "direction": "in"},
        {"type": "msg", "dataId": "b", "pre": "[09:05, 01/09/2026] X: ", "text": "yo", "time": "", "direction": "in"},
    ]
    msgs = w.build_chat_snapshot(units, "X", now_local=NOW, tz=TZ)["messages"]
    assert msgs[0]["created_at"] == "2026-09-01T07:00:00.000000+00:00"
    assert msgs[0]["time_source"] == "inferred"


def test_save_snapshot_writes_one_json_per_chat(page, tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(w, "CHAT_SNAPSHOT_DIR", str(tmp_path))
    snap = _snapshot(page)
    path = w.save_snapshot(snap)
    assert path == tmp_path / f"{w.chat_slug('Karl')}.json"
    assert json.loads(path.read_text(encoding="utf-8")) == snap


# ── sidebar ──────────────────────────────────────────────────────────────

def _sidebar(rows):
    # absolutely positioned like WhatsApp's virtual list, DOM order scrambled
    items = "".join(
        f'<div data-testid="cell-frame-container" role="listitem" '
        f'style="position:absolute;top:{top}px;height:60px;width:100%">'
        f'<div data-testid="cell-frame-title">'
        + (f'<span style="position:absolute;clip:rect(0 0 0 0);width:1px;height:1px;overflow:hidden">'
           f'{unread} unread message{"s" if unread > 1 else ""}</span>' if unread else "")
        + f'<span dir="auto">{name}</span></div>'
        f'<span data-testid="cell-frame-secondary">{preview}</span></div>'
        for name, preview, top, unread in rows)
    return (f'<div id="pane-side" style="position:relative;height:300px;overflow:auto">'
            f'<div style="position:relative;height:{60 * len(rows)}px">{items}</div></div>')


def test_recent_rows_are_real_names_in_screen_order(page):
    page.set_content(_sidebar([
        ("Mai Muzunze", "Ndsrwadzowa", 240, 0),
        ("Mr NDERE BUILDER", "Morning", 0, 1),
        ("SCS1101-Aug'26", "2 photos", 120, 2),
        ("Karl", "Gd mng", 60, 1),
        ("Mukoma Mole WhatsApp", "Chazukuru kwakadi", 180, 1),
    ]))
    rows = w.get_recent_chat_rows(page, 3)
    assert [r["name"] for r in rows] == ["Mr NDERE BUILDER", "Karl", "SCS1101-Aug'26"]
    all5 = w.get_recent_chat_rows(page, 15)
    assert [r["name"] for r in all5] == [
        "Mr NDERE BUILDER", "Karl", "SCS1101-Aug'26", "Mukoma Mole WhatsApp", "Mai Muzunze"]


def _ambiguous(divider, pre_date):
    return [
        {"type": "divider", "text": divider, "y": 0},
        {"type": "msg", "dataId": "a", "pre": f"[10:00, {pre_date}] X: ", "text": "hi", "time": "", "direction": "in", "y": 1},
    ]


def test_ambiguous_dates_follow_the_chats_own_dividers():
    # 05/09/2026: 5 September (day-first) or May 9 (month-first)?
    dayfirst = w.build_chat_snapshot(_ambiguous("5 September 2026", "05/09/2026"), "X", now_local=NOW, tz=TZ)
    assert dayfirst["messages"][0]["created_at"].startswith("2026-09-05T08:00")
    monthfirst = w.build_chat_snapshot(_ambiguous("September 5, 2026", "09/05/2026"), "X", now_local=NOW, tz=TZ)
    assert monthfirst["messages"][0]["created_at"].startswith("2026-09-05T08:00")


def test_a_reading_that_puts_messages_in_the_future_is_refused():
    # 09/12/2026 day-first = 9 Dec 2026, after NOW (30 Sep) -> must be 12 Sep
    units = [{"type": "msg", "dataId": "a", "pre": "[10:00, 09/12/2026] X: ", "text": "hi",
              "time": "", "direction": "in", "y": 1}]
    snap = w.build_chat_snapshot(units, "X", now_local=NOW, tz=TZ)
    assert snap["messages"][0]["created_at"].startswith("2026-09-12T08:00")
