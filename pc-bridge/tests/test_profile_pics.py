"""Profile pictures: read from the open chat's header, uploaded only when the
server doesn't have that exact picture."""
import http.server
import struct
import threading
import zlib

import pytest

import wa_bridge as w


def _png(width, height, rgb=(200, 30, 30)):
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


@pytest.fixture(scope="module")
def cdn():
    """Stands in for WhatsApp's photo CDN (pps.whatsapp.net)."""
    pics = {"/karl.png": _png(640, 640), "/karl-again.png": _png(640, 640), "/nomsa.png": _png(96, 96, (0, 90, 200))}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = pics.get(self.path.split("?")[0])
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _header(page, avatar_src=None, emoji=True):
    emoji_img = ('<img class="emoji" src="data:image/png;base64,iVBORw0KGgo=" '
                 'style="width:16px;height:16px">' if emoji else "")
    avatar = (f'<img src="{avatar_src}" style="width:40px;height:40px;border-radius:50%">' if avatar_src
              else '<span data-icon="default-user"><svg width="40" height="40"></svg></span>')
    page.set_content(f'<div id="main"><header>{avatar}<span>Karl {emoji_img}</span></header></div>')


@pytest.fixture()
def uploads(monkeypatch):
    sent = []
    monkeypatch.setattr(w, "_pic_cache", {})
    monkeypatch.setattr(w, "_pic_endpoint_missing", False)

    def fake_upload(chat_id, jpeg):
        sent.append((chat_id, jpeg))
        return w.hashlib.sha1(jpeg).hexdigest()[:12]
    monkeypatch.setattr(w, "upload_profile_pic", fake_upload)
    return sent


def test_reads_the_avatar_not_an_emoji_in_the_name(page, cdn):
    _header(page, f"{cdn}/karl.png")
    assert w.get_header_avatar_src(page) == f"{cdn}/karl.png"
    assert w._fetch_avatar_bytes(page, f"{cdn}/karl.png") == _png(640, 640)   # full size, not a screenshot


def test_no_photo_keeps_the_initials(page, uploads):
    _header(page, None)
    assert w.get_header_avatar_src(page) is None
    assert w.sync_profile_pic(page, "karl", None) is False and uploads == []


def test_uploads_a_small_jpeg_once_and_again_only_when_the_server_lost_it(page, cdn, uploads):
    _header(page, f"{cdn}/karl.png")
    assert w.sync_profile_pic(page, "karl", None) is True              # server has none
    chat_id, jpeg = uploads[0]
    assert chat_id == "karl" and jpeg[:3] == b"\xff\xd8\xff"          # converted to JPEG
    width, height = page.evaluate("""async (b64) => { const i = new Image();
        i.src = 'data:image/jpeg;base64,' + b64; await i.decode(); return [i.naturalWidth, i.naturalHeight]; }""",
                                  w.base64.b64encode(jpeg).decode())
    assert max(width, height) <= 320                                   # resized from 640
    version = w.hashlib.sha1(jpeg).hexdigest()[:12]

    assert w.sync_profile_pic(page, "karl", version) is False         # server has it: nothing sent
    assert w.sync_profile_pic(page, "karl") is False                  # no info: trust our last upload
    assert w.sync_profile_pic(page, "karl", None) is True             # Space restarted: re-upload
    assert len(uploads) == 2

    _header(page, f"{cdn}/karl-again.png")                            # new signed URL, same picture
    assert w.sync_profile_pic(page, "karl", version) is False and len(uploads) == 2

    _header(page, f"{cdn}/nomsa.png")                                 # picture really changed
    assert w.sync_profile_pic(page, "karl", version) is True and len(uploads) == 3


def test_falls_back_to_a_screenshot_when_the_photo_url_fails(page, cdn, uploads):
    _header(page, f"{cdn}/expired.png")                               # 404 from the CDN
    assert w.sync_profile_pic(page, "karl", None) is True
    assert uploads[0][1][:3] == b"\xff\xd8\xff"


def test_upload_stops_trying_when_the_server_has_no_endpoint(monkeypatch):
    monkeypatch.setattr(w, "_pic_endpoint_missing", False)

    class R:
        status_code, text, ok = 404, '{"detail":"Not Found"}', False

    class S:
        def post(self, *a, **k):
            return R()
    monkeypatch.setattr(w, "get_session", lambda: S())
    assert w.upload_profile_pic("karl", b"\xff\xd8\xff") is None
    assert w._pic_endpoint_missing is True
