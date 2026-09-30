"""A chat modelled on the 30/09 WhatsApp DOM report: bare message ids, message
content drawn ONLY near the visible part of the list (placeholders elsewhere),
no data-id prefixes, your photo identifiable only by its delivery ticks, a
quoted reply, and a group header whose only titled span is the member list."""
import json
from datetime import datetime, timedelta, timezone

import wa_bridge as w

TZ = timezone(timedelta(hours=2))
NOW = datetime(2026, 9, 30, 14, 30)
IMG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
N = 60


def _spec():
    rows = [{"divider": "YESTERDAY"}]
    for i in range(N):
        if i == 30:
            rows.append({"divider": "TODAY"})
        day = "29/09/2026" if i < 30 else "30/09/2026"
        hhmm = f"{10 + i // 60:02d}:{i % 60:02d}"
        out = i % 2 == 1
        m = {"id": f"A{i:04d}C0FFEE", "out": out, "time": hhmm,
             "pre": f"[{hhmm}, {day}] {'Karl' if out else '+27 66 265 0786'}: ",
             "text": f"{'out' if out else 'in'} {i}"}
        if i == 45:                                  # your photo: no metadata, ticks only
            m.update(pre="", text="", photo=True, out=True)
        if i == 46:                                  # a reply quoting your message
            m.update(quote=["You", "out 45 caption"], text="Yes, we are 3", out=False)
        rows.append(m)
    return rows


PAGE = """
<div id="main" style="width:900px">
 <header data-testid="conversation-header"><div>
  <div role="button" aria-label="Profile details" title="Profile details"><img></div>
  <div role="button" data-testid="conversation-info-header"><div><div>
     <span data-testid="conversation-info-header" dir="auto">+27 66 265 0786</span></div></div>
   <div data-testid="chat-subtitle"><span data-testid="selectable-text" title="click here for contact info">x</span></div>
  </div></div></header>
 <div id="panel" data-testid="conversation-panel-messages" style="height:480px;overflow-y:auto"></div>
</div>
<script>
const SPEC = __SPEC__;
const panel = document.getElementById('panel');
const rows = SPEC.map(s => {
  const r = document.createElement('div');
  r.setAttribute('role', 'row');
  r.style.height = '80px';
  if (s.divider) { r.innerHTML = '<span>' + s.divider + '</span>'; }
  else {
    const m = document.createElement('div');
    m.setAttribute('data-id', s.id);
    m.setAttribute('data-testid', 'conv-msg-' + s.id);
    m.style.cssText = 'display:flex;height:80px;justify-content:' + (s.out ? 'flex-end' : 'flex-start');
    r.appendChild(m);
  }
  panel.appendChild(r);
  return r;
});
function content(s) {
  const ticks = s.out ? '<span data-icon="msg-dblcheck"></span>' : '';
  const meta = '<span data-testid="msg-meta"><span>' + s.time + '</span>' + ticks + '</span>';
  if (s.photo) return '<div><img src="' + '""" + IMG + """' + '" style="width:200px;height:60px">' + meta + '</div>';
  const quote = s.quote ? '<div role="button"><span>' + s.quote[0] + '</span><br><span>' + s.quote[1] + '</span></div>' : '';
  return '<div style="max-width:60%"><div class="copyable-text" data-pre-plain-text="' + s.pre + '">' + quote +
         '<span class="selectable-text">' + s.text + '</span></div>' + meta + '</div>';
}
// WhatsApp-style virtualisation: only rows within one screen of the viewport
// have content; the rest are empty placeholders (id kept, height kept).
function draw() {
  const top = panel.scrollTop, h = panel.clientHeight;
  rows.forEach((r, i) => {
    const s = SPEC[i]; if (s.divider) return;
    const m = r.firstChild, y = r.offsetTop - panel.offsetTop;
    const near = y > top - h && y < top + 2 * h;
    if (near && !m.innerHTML) m.innerHTML = content(s);
    if (!near && m.innerHTML) m.innerHTML = '';
  });
}
panel.addEventListener('scroll', draw);
panel.scrollTop = panel.scrollHeight; draw();
</script>
"""


def _load(page):
    page.set_content(PAGE.replace("__SPEC__", json.dumps(_spec())))


def test_single_position_read_sees_placeholders(page):
    """What earlier builds did: one read of the list -> most messages blank."""
    _load(page)
    units = w.walk_chat(page)
    shells = [u for u in units if u["type"] == "msg" and u["shell"]]
    assert len(shells) > 40


def test_full_read_gets_every_message_with_correct_side(page):
    _load(page)
    units, _ = w.collect_units(page, "full", max_scroll_rounds=2)
    snap = w.build_chat_snapshot(units, "+27662650786", now_local=NOW, tz=TZ)
    msgs = snap["messages"]
    assert len(msgs) == N and snap["stats"]["not_rendered"] == 0 and snap["complete"]
    assert not snap["rejected"]
    by_id = {m["wa_id"]: m for m in msgs}
    for i in range(N):
        m = by_id[f"A{i:04d}C0FFEE"]
        if i == 45:
            assert (m["kind"], m["direction"], m["sender"]) == ("image", "out", "You (WhatsApp)")
        elif i == 46:
            assert (m["text"], m["direction"]) == ("Yes, we are 3", "in")      # quote stripped
        else:
            assert m["direction"] == ("out" if i % 2 else "in"), i
            assert m["text"] == f"{'out' if i % 2 else 'in'} {i}"
    assert [m["seq"] for m in msgs] == list(range(N))
    assert [m["wa_id"] for m in msgs] == [f"A{i:04d}C0FFEE" for i in range(N)]    # conversation order
    assert snap["window_start"] == msgs[0]["created_at"]


def test_recent_read_covers_only_the_last_screens_and_is_fast(page):
    _load(page)
    units, _ = w.collect_units(page, "recent")
    snap = w.build_chat_snapshot(units, "+27662650786", now_local=NOW, tz=TZ)
    ids = [m["wa_id"] for m in snap["messages"]]
    assert ids[-1] == f"A{N - 1:04d}C0FFEE"                 # newest message read
    assert 10 <= len(ids) < N                               # not the whole chat
    # placeholders above the recent window never drawn -> pruning starts at the
    # first message that WAS read, never earlier
    first_read = snap["messages"][0]
    assert snap["window_start"] == first_read["created_at"]
    assert snap["stats"]["not_rendered"] > 0


def test_never_drawn_message_blocks_pruning_before_it():
    units = [
        {"type": "msg", "dataId": "a", "pre": "[10:00, 29/09/2026] X: ", "text": "old", "time": "", "direction": "in"},
        {"type": "msg", "dataId": "b", "shell": True, "pre": "", "text": "", "time": "", "direction": "unknown"},
        {"type": "msg", "dataId": "c", "pre": "[10:05, 29/09/2026] X: ", "text": "new", "time": "", "direction": "in"},
    ]
    snap = w.build_chat_snapshot(units, "X", now_local=NOW, tz=TZ)
    assert [m["text"] for m in snap["messages"]] == ["old", "new"]
    assert snap["window_start"] == snap["messages"][1]["created_at"]    # after the gap
    assert not snap["complete"] and not snap["rejected"]
    tail_gap = units[:1] + [dict(units[1], dataId="z")]
    assert w.build_chat_snapshot(tail_gap, "X", now_local=NOW, tz=TZ)["window_start"] is None


def test_photo_far_from_view_is_scrolled_to_and_read(page):
    _load(page)
    panel = w._panel_handle(page)
    panel.evaluate("el => { el.scrollTop = 0; }")          # photo (row 46) now a placeholder
    page.wait_for_timeout(200)
    assert w._image_b64_for(page, "A0045C0FFEE")


def test_group_header_identity_uses_the_name_not_the_member_list(page):
    page.set_content("""<div id="main"><header data-testid="conversation-header"><div>
      <div role="button" aria-label="Profile details" title="Profile details"><img></div>
      <div role="button" data-testid="conversation-info-header"><div><div>
        <span data-testid="conversation-info-header" dir="auto">ECW1102 ENGINEERING DRAWING</span></div></div>
      <div data-testid="chat-subtitle"><span data-testid="selectable-text"
           title="Anele, Class, Denzel, +263 78 586 7800, You">Anele, Class, …</span></div></div></div></header></div>""")
    assert w.header_shows(page, "ECW1102 ENGINEERING DRAWING") is True
    assert w.header_shows(page, "Somebody else") is False
