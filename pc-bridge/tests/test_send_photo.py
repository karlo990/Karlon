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
  // WhatsApp's menu has a STICKER input too, and it comes first in the DOM.
  const st = document.createElement('input'); st.type = 'file'; st.id = 'sticker'; st.accept = 'image/*';
  st.style.display = 'none';
  st.onchange = () => window.SENT.push('WRONG: sent as sticker');
  document.body.appendChild(st);
  const i = document.createElement('input'); i.type = 'file'; i.id = 'file'; i.accept = 'image/*,video/mp4,video/3gpp,video/quicktime';
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


def _webp(page, tmp_path):
    b64 = page.evaluate("""() => { const c = document.createElement('canvas'); c.width = 40; c.height = 30;
        const g = c.getContext('2d'); g.fillStyle = '#c33'; g.fillRect(0, 0, 40, 30);
        return c.toDataURL('image/webp').split(',')[1]; }""")
    import base64
    f = tmp_path / "listing.jpg"          # CDN names it .jpg, bytes are WebP
    f.write_bytes(base64.b64decode(b64))
    assert w._image_format(f.read_bytes()) == "webp"
    return f


def test_webp_photo_is_converted_to_real_jpeg(page, tmp_path):
    page.set_content("<p>x</p>")
    out = w.ensure_jpeg(page, str(_webp(page, tmp_path)))
    data = open(out, "rb").read()
    assert out.endswith(".jpg") and w._image_format(data) == "jpeg"


def test_webp_conversion_works_without_pillow(page, tmp_path, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "PIL", None)          # import PIL -> ImportError
    page.set_content("<p>x</p>")
    out = w.ensure_jpeg(page, str(_webp(page, tmp_path)))
    assert w._image_format(open(out, "rb").read()) == "jpeg"


MENU_PAGE = """
<div id="main"><footer>
  <div id="compose" contenteditable="true" data-tab="10" aria-label="Type a message" style="width:600px;height:40px"></div>
  <button aria-label="Attach" id="attach" style="width:40px;height:40px">+</button>
</footer></div>
<div id="menu" style="display:none">
  <div role="button" id="m-doc"><span>Document</span></div>
  <div role="button" id="m-photos"><span>Photos &amp; videos</span></div>
  <div role="button" id="m-sticker"><span>New sticker</span></div>
</div>
<input type="file" id="sticker" accept="image/*" style="display:none">
<div id="editor" style="display:none;position:fixed;inset:0;background:#fff">
  <div role="button" aria-label="Send" id="editor-send" style="position:absolute;bottom:10px;right:10px;width:48px;height:48px">
    <span data-icon="wds-ic-send-filled" style="display:block;width:24px;height:24px"></span></div>
</div>
<script>
window.SENT = [];
const editor = document.getElementById('editor');
document.getElementById('attach').onclick = () => { document.getElementById('menu').style.display = 'block'; };
document.getElementById('sticker').onchange = () => { window.SENT.push('WRONG: sticker'); editor.style.display = 'block'; };
document.getElementById('m-sticker').onclick = () => document.getElementById('sticker').click();
document.getElementById('m-photos').onclick = () => {
  // current WhatsApp: the photos input only exists once its menu item is clicked
  const i = document.createElement('input'); i.type = 'file'; i.accept = 'image/*,video/mp4';
  i.style.display = 'none'; i.onchange = () => { window.SENT.push('photo'); editor.style.display = 'block'; };
  document.body.appendChild(i); i.click();
};
document.getElementById('editor-send').onclick = () => { window.SENT.push('sent'); editor.remove(); };
</script>
"""


def test_photo_goes_through_photos_and_videos_never_the_sticker_input(page, tmp_path):
    page.set_content(MENU_PAGE)
    assert w.dom_send_file(page, _photo(tmp_path), kind="image")
    assert page.evaluate("window.SENT") == ["photo", "sent"]


def test_no_photos_option_fails_instead_of_sending_a_sticker(page, tmp_path):
    page.set_content(MENU_PAGE.replace('<span>Photos &amp; videos</span>', '<span>Gallery</span>'))
    assert not w.dom_send_file(page, _photo(tmp_path), kind="image")
    assert page.evaluate("window.SENT") == []


def test_multiline_text_is_one_message(page):
    page.set_content('<div id="box" contenteditable="true" style="width:500px;height:200px"></div>'
                     '<script>window.SENDS = 0; document.getElementById("box").addEventListener("keydown", e => {'
                     ' if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); window.SENDS++; } });</script>')
    page.click("#box")
    w.type_multiline(page, "This is what I have found for you:\n🏠 KCER 116\n📍 Hatfield, Harare")
    assert page.evaluate("window.SENDS") == 0                      # no line was sent on its own
    assert page.evaluate("document.getElementById('box').innerText").count("KCER 116") == 1


def test_pdf_goes_through_document_option_with_its_real_name(page, tmp_path):
    doc_page = MENU_PAGE.replace("document.getElementById('editor-send').onclick", """document.getElementById('m-doc').onclick = () => {
  const i = document.createElement('input'); i.type = 'file'; i.accept = '*';
  i.style.display = 'none'; i.onchange = () => { window.SENT.push('doc:' + i.files[0].name); editor.style.display = 'block'; };
  document.body.appendChild(i); i.click();
};
document.getElementById('editor-send').onclick""")
    page.set_content(doc_page)
    pdf = tmp_path / "KARLCON_Invoice_KCER-2026-0010.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    assert w.dom_send_file(page, str(pdf), kind="document")
    assert page.evaluate("window.SENT") == ["doc:KARLCON_Invoice_KCER-2026-0010.pdf", "sent"]


def test_download_keeps_the_server_file_name(monkeypatch):
    class R:
        content, headers = b"%PDF-1.4", {"content-type": "application/pdf"}

        def raise_for_status(self):
            pass

    class S:
        def get(self, *a, **k):
            return R()
    monkeypatch.setattr(w, "get_session", lambda: S())
    path = w.download_to_temp("/static/invoices/KARLCON_Invoice_KCER-2026-0010.pdf")
    import os
    assert os.path.basename(path) == "KARLCON_Invoice_KCER-2026-0010.pdf"
    os.remove(path)
    os.rmdir(os.path.dirname(path))
