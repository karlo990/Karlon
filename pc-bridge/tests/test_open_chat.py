"""Opening a chat on a WhatsApp-like page that has NONE of the data-testid /
role="application" markers — the situation in the 30/09 log, where every chat
failed with "could not open" although the clicks worked."""
import wa_bridge as w

CHATS = {
    "Mom": [("false_1@c.us_A", "[08:00, 30/09/2026] Mom: ", "Morning dear", "left")],
    "+263780438459": [("true_2@c.us_B", "[08:05, 30/09/2026] Karlo: ", "On my way", "right")],
}

PAGE = """
<div id="side" style="position:absolute;left:0;top:0;width:300px">
  <div id="pane-side" style="height:400px;overflow:auto">
    <div role="listitem" style="height:60px" data-chat="Mom"><span dir="auto" title="Mom">Mom</span></div>
    <div role="listitem" style="height:60px" data-chat="+263780438459">
      <span dir="auto" title="‪+263 78 043 8459‬">+263 78 043 8459</span></div>
  </div>
</div>
<div id="app-main" style="position:absolute;left:320px;top:0;width:800px"></div>
<script>
const CHATS = __CHATS__;
document.querySelectorAll('[role="listitem"]').forEach(r => r.addEventListener('click', () => {
  const name = r.getAttribute('data-chat');
  setTimeout(() => {   // WhatsApp renders the chat a moment after the click
    const msgs = CHATS[name].map(([id, pre, text, side]) =>
      `<div style="display:flex;justify-content:${side === 'right' ? 'flex-end' : 'flex-start'}">
         <div data-id="${id}" style="max-width:60%"><div class="copyable-text" data-pre-plain-text="${pre}">
         <span class="selectable-text">${text}</span></div></div></div>`).join('');
    document.getElementById('app-main').innerHTML =
      `<div id="main"><header><span title="${name === 'Mom' ? 'Mom' : '+263 78 043 8459'}">x</span></header>
       <div style="height:500px;overflow-y:auto"><div style="height:900px">${msgs}</div></div></div>`;
  }, 300);
}));
</script>
"""


def _load(page):
    import json
    page.set_content(PAGE.replace("__CHATS__", json.dumps(CHATS)))


def test_opens_and_reads_chats_without_data_testid_markers(page):
    _load(page)
    names = [r["name"] for r in w.get_chat_rows(page)]
    assert names == ["Mom", "+263780438459"]            # phone number in one canonical form
    for name, expected in CHATS.items():
        assert w.open_chat_row(page, name), name
        assert w.get_open_chat_title(page) == name
        snap = w.scan_chat(page, name, max_scroll_rounds=2)
        assert [m["text"] for m in snap["messages"]] == [e[2] for e in expected]
        assert snap["chat"]["id"] == w.chat_slug(name)


def test_wrong_chat_is_not_accepted_as_open(page):
    _load(page)
    assert w.open_chat_row(page, "Mom")
    # "Nobody" has no row: must fail, not report the still-open "Mom" as success
    assert not w._wait_chat_open(page, "Nobody", timeout_s=1)


def test_dom_report_lists_markers(page):
    _load(page)
    w.open_chat_row(page, "Mom")
    rep = w.dom_report(page)
    assert "#main [data-id]" in rep and "header titles: ['Mom']" in rep
