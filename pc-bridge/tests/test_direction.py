"""Direction detection + scrape contract, run against a fake WhatsApp-style DOM.

The DOM here is a *model* of WhatsApp Web's message markup (data-id,
data-pre-plain-text, aria-label sender spans, tail icons, flex alignment) —
it proves the logic handles each signal and each fallback, not that WhatsApp's
live markup today still matches. Run `wa_bridge.py --discover` against the real
thing for that.
"""
import wa_bridge as w


def _msg(data_id, sender, text, ts, side, aria=None, tail=None):
    justify = "flex-end" if side == "right" else "flex-start"
    aria_html = f'<span aria-label="{aria}"></span>' if aria else ""
    tail_html = f'<span data-icon="{tail}"></span>' if tail else ""
    return (
        f'<div data-id="{data_id}" style="display:flex;justify-content:{justify};margin:4px">'
        f'<div style="max-width:60%;border:1px solid #ccc">{aria_html}{tail_html}'
        f'<div class="copyable-text" data-pre-plain-text="[{ts}] {sender}: ">'
        f'<span class="selectable-text">{text}</span></div></div></div>'
    )


def _chat_html(rows):
    return (
        '<div id="main" style="width:860px">'
        '<div data-testid="conversation-panel-messages" '
        'style="width:860px;height:600px;overflow:auto">' + "".join(rows) + "</div></div>"
    )


# (data_id, sender printed by WA, text, side, aria, tail) -> expected (direction, via)
CASES = [
    ("false_1@c.us_A1", "Thabo", "hello",       "left",  "Thabo:", None,       ("in",  "aria")),
    ("true_1@c.us_A2",  "Karlo", "hi Thabo",    "right", "You:",   None,       ("out", "aria")),
    ("x_A3",            "Karlo", "tail out",    "right", None,     "tail-out", ("out", "tail")),
    ("false_1@c.us_A4", "Thabo", "id prefix",   "left",  None,     None,       ("in",  "data-id")),
    ("true_1@c.us_A5",  "Karlo", "id prefix 2", "right", None,     None,       ("out", "data-id")),
    ("x_A6",            "Karlo", "geo right",   "right", None,     None,       ("out", "geometry")),
    ("x_A7",            "Thabo", "geo left",    "left",  None,     None,       ("in",  "geometry")),
]


def test_probe_each_signal_and_fallback(page):
    rows = [_msg(d, s, t, "10:1%d, 12/03/2025" % i, side, a, tl)
            for i, (d, s, t, side, a, tl, _) in enumerate(CASES)]
    page.set_content(_chat_html(rows))
    els = page.query_selector_all("[data-pre-plain-text]")
    assert len(els) == len(CASES)
    for el, case in zip(els, CASES):
        got = w.probe_message(el)
        assert (got["direction"], got["via"]) == case[-1], case
        assert got["dataId"] == case[0]


def test_geometry_refuses_dead_centre(page):
    row = (
        '<div data-id="x_C" style="display:flex;justify-content:center">'
        '<div class="copyable-text" data-pre-plain-text="[10:00, 12/03/2025] Karlo: ">'
        '<span class="selectable-text">centred</span></div></div>'
    )
    page.set_content(_chat_html([row]))
    got = w.probe_message(page.query_selector("[data-pre-plain-text]"))
    assert got["direction"] == "unknown"


def test_scrape_messages_only_emits_in_or_out_and_no_duplicates(page):
    rows = [_msg(d, s, t, "10:1%d, 12/03/2025" % i, side, a, tl)
            for i, (d, s, t, side, a, tl, _) in enumerate(CASES)]
    # Dead-centre, no signals, but printed under the same name as known 'out'
    # messages ("Karlo") -> the sender vote must resolve it to 'out'.
    rows.append(
        '<div data-id="x_C" style="display:flex;justify-content:center">'
        '<div class="copyable-text" data-pre-plain-text="[10:30, 12/03/2025] Karlo: ">'
        '<span class="selectable-text">centred by me</span></div></div>'
    )
    # Dead-centre, no signals, unknown sender -> safe default 'in'.
    rows.append(
        '<div data-id="x_D" style="display:flex;justify-content:center">'
        '<div class="copyable-text" data-pre-plain-text="[10:31, 12/03/2025] Someone: ">'
        '<span class="selectable-text">centred stranger</span></div></div>'
    )
    page.set_content(_chat_html(rows))

    out = w.scrape_messages(page, "Thabo", max_scroll_rounds=4)

    n = len(CASES) + 2
    assert len(out) == n, [o["text"] for o in out]                # no double import
    assert len({o["external_key"] for o in out}) == n
    assert all(o["direction"] in ("in", "out") for o in out)      # binary contract
    assert all(o["created_at"] is not None for o in out)          # timestamped pass, not the fallback
    by_text = {o["text"]: o for o in out}
    for d, s, t, side, a, tl, (direction, via) in CASES:
        assert by_text[t]["direction"] == direction, (t, via)
    assert by_text["centred by me"]["direction"] == "out"
    assert by_text["centred stranger"]["direction"] == "in"
    for o in out:
        if o["direction"] == "out":
            assert o["sender"] == "You (WhatsApp)"
        else:
            assert o["sender"] and o["sender"] != "You (WhatsApp)"
    assert "raw_sender" not in out[0]


def test_row_fallback_still_used_when_no_timestamp_metadata(page):
    # A message with no data-pre-plain-text (e.g. WA changed the attribute)
    # must still be imported through the data-id row pass.
    row = (
        '<div data-id="true_9@c.us_Z" style="display:flex;justify-content:flex-end">'
        '<span aria-label="You:"></span><span class="selectable-text">no metadata</span></div>'
    )
    page.set_content(_chat_html([row]))
    out = w.scrape_messages(page, "Thabo", max_scroll_rounds=4)
    assert [(o["text"], o["direction"], o["created_at"]) for o in out] == [("no metadata", "out", None)]


def test_resolve_directions_pure():
    e = [
        {"raw_sender": "Karlo", "direction": "out"},
        {"raw_sender": "Karlo", "direction": "unknown"},
        {"raw_sender": "Thabo", "direction": "in"},
        {"raw_sender": "Thabo", "direction": "unknown"},
        {"raw_sender": "", "direction": "unknown"},
        {"raw_sender": "Mixed", "direction": "in"},
        {"raw_sender": "Mixed", "direction": "out"},
        {"raw_sender": "Mixed", "direction": "unknown"},   # tie -> default 'in'
    ]
    w.resolve_directions(e)
    assert [x["direction"] for x in e] == ["out", "out", "in", "in", "in", "in", "out", "in"]
