"""Sending a photo through a WhatsApp-style editor that opens ON TOP of the
chat (the 02/10 log: every send click blocked because the bridge typed the
caption into, and clicked the send button of, the chat box hidden behind the
editor)."""
import time

import wa_bridge as w

PAGE = """
<style>body{margin:0} #main{position:relative;width:1000px;height:700px}
 footer{position:absolute;bottom:0;left:0;right:0;height:60px;display:flex;align-items:center}
 #editor{position:fixed;inset:0;background:#fff;display:none}
 #editor .bar{position:absolute;bottom:0;left:0;right:0;height:60px;display:flex;align-items:center}
</style>
<div id="main"><header><span dir="auto">ROYAL CREST ACADEMY ADVOCATES</span></header>
 <footer>
  <div id="compose" contenteditable="true" data-tab="10" aria-label="Type a message" style="flex:1;height:40px"></div>
  <button aria-label="Attach" id="attach" style="width:40px;height:40px">+</button>
  <div role="button" aria-label="Send" id="chat-send" style="width:48px;height:48px">
    <span data-icon="wds-ic-send-filled" style="display:block;width:24px;height:24px"></span></div>
 </footer>
</div>
<div id="editor">
  <img id="preview" style="width:300px;height:300px;margin:100px">
  <div class="bar">
    <div id="caption" contenteditable="true" aria-label="Type a message" style="flex:1;height:40px"></div>
    <div role="button" aria-label="Send" id="editor-send" style="width:48px;height:48px">
      <span data-icon="wds-ic-send-filled" style="display:block;width:24px;height:24px"></span></div>
  </div>
</div>
<script>
window.SENT = [];
window.BROKEN = false;
const editor = document.getElementById('editor');
document.getElementById('attach').onclick = () => {
  if (document.getElementById('file')) return;
  const i = document.createElement('input'); i.type = 'file'; i.id = 'file'; i.accept = 'image/*,video/mp4';
  i.style.display = 'none';
  i.onchange = () => { editor.style.display = 'block'; };
  document.body.appendChild(i);
};
document.getElementById('editor-send').onclick = () => {
  if (window.BROKEN) return;                                  // WhatsApp ignoring the click
  window.SENT.push(document.getElementById('caption').innerText);
  editor.remove();                                            // editor closes on send
};
document.getElementById('chat-send').onclick = () => window.SENT.push('WRONG: chat send ' + document.getElementById('compose').innerText);
document.addEventListener('keydown', e => { if (e.key === 'Escape' && document.getElementById('editor')) editor.style.display = 'none'; });
</script>
"""


def _photo(tmp_path):
    f = tmp_path / "house.png"
    f.write_bytes(b"\\x89PNG\\r\\n\\x1a\\n" + b"0" * 64)
    return str(f)


def test_photo_is_sent_from_the_editor_with_caption_in_the_editor(page, tmp_path):
    page.set_content(PAGE)
    ok = w.dom_send_file(page, _photo(tmp_path), caption="3 bedrooms, Borrowdale", kind="image")
    assert ok
    assert page.evaluate("window.SENT") == ["3 bedrooms, Borrowdale"]
    assert page.evaluate("document.getElementById('compose').innerText") == ""   # chat box untouched


def test_ignored_send_fails_fast_and_closes_the_editor(page, tmp_path):
    page.set_content(PAGE)
    page.evaluate("window.BROKEN = true")
    t0 = time.time()
    ok = w.dom_send_file(page, _photo(tmp_path), caption="x", kind="image")
    assert not ok
    assert time.time() - t0 < 20                     # was 30 s per blocked click
    assert page.evaluate("window.SENT") == []
    assert page.evaluate("getComputedStyle(document.getElementById('editor')).display") == "none"


def test_header_match_ignores_emoji_and_case(page):
    page.set_content(PAGE)
    assert w.header_shows(page, "ROYAL CREST ACADEMY ADVOCATES 🎓") is True
    assert w.header_shows(page, "Royal Crest Academy Advocates") is True
    assert w.header_shows(page, "ROYAL CREST") is False
