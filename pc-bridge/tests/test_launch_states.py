"""Starting up: whatever WhatsApp Web shows first, the bridge waits for it
properly instead of timing out after 30 s and crash-looping."""
import wa_bridge as w

CHATS = ("<div id=pane-side style=height:200px><div role=listitem style=height:50px>"
         "<span title=Karl>Karl</span></div></div>")


def _swap(html, ms):
    return f"<script>setTimeout(() => {{ document.body.innerHTML = \"{CHATS}\"; }}, {ms});</script>{html}"


def _wait(page, **kw):
    kw.setdefault("poll", 0.1)
    kw.setdefault("report_every", 0.3)
    return w.wait_for_whatsapp(page, **kw)


def test_login_screen_without_a_recognised_qr_waits_for_the_scan(page, tmp_path, capsys):
    # The 5 Oct log: a login page whose QR markup didn't match the old selector.
    # A text-only login page, which turns into the chat list 1.5 s later.
    page.set_content(_swap("<h1>Log in to WhatsApp Web</h1><p>Scan the QR code</p>", 1500))
    # load_timeout is shorter than the scan takes: the QR phase must not be cut by it.
    assert _wait(page, load_timeout=0.5, login_timeout=6, out_dir=str(tmp_path))
    out = capsys.readouterr().out
    assert "asking you to log in" in out and "Linked devices" in out


def test_qr_canvas_is_recognised(page):
    page.set_content("<canvas aria-label='Scan this QR code to link a device!' width=50 height=50></canvas>")
    assert w.whatsapp_state(page)[0] == "qr"


def test_open_in_another_window_is_handled_with_use_here(page, tmp_path, capsys):
    page.set_content("<div>WhatsApp is open in another window. Click 'Use here' to use WhatsApp in this window."
                     f"</div><button onclick='document.body.innerHTML = \"{CHATS}\"'>Use here</button>")
    assert w.whatsapp_state(page)[0] == "another_window"
    assert _wait(page, load_timeout=5, out_dir=str(tmp_path))
    assert "Use here" in capsys.readouterr().out


def test_slow_loading_screen_is_waited_out(page, tmp_path):
    page.set_content(_swap("<div>Loading your chats</div>", 1200))
    assert w.whatsapp_state(page)[0] == "loading"
    assert _wait(page, load_timeout=6, out_dir=str(tmp_path))


def test_a_page_that_never_gets_there_fails_with_a_screenshot(page, tmp_path, capsys):
    page.set_content("<p>Something unexpected</p>")
    assert not _wait(page, load_timeout=0.6, out_dir=str(tmp_path))
    assert list(tmp_path.glob("wa_launch_failed_*.png")) and list(tmp_path.glob("wa_launch_failed_*.txt"))
    assert "Something unexpected" in capsys.readouterr().out


def test_empty_chat_list_is_explained_not_synced_silently(page, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(w, "_empty_reports", 0)
    monkeypatch.setattr(w, "LOG_DIR", str(tmp_path))
    page.set_content("<div id='pane-side' style='height:100px'>Loading your chats</div>")
    w._explain_empty_sidebar(page)
    out = capsys.readouterr().out
    assert "no chats visible" in out and "still loading" in out
    w._explain_empty_sidebar(page)                       # not repeated every 5 s pass
    assert capsys.readouterr().out == ""


def test_session_folder_is_stable_across_updates(tmp_path, monkeypatch):
    monkeypatch.setenv("WA_SESSION_DIR", str(tmp_path / "keep"))
    assert w._default_session_dir() == str((tmp_path / "keep").resolve())
    monkeypatch.delenv("WA_SESSION_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    assert w._default_session_dir().endswith(str(w.Path(".karlon") / "wa_session"))    # new install
    (tmp_path / "wa_session").mkdir()
    assert w._default_session_dir() == str((tmp_path / "wa_session").resolve())        # old login kept
    (tmp_path / "home" / ".karlon" / "wa_session").mkdir(parents=True)
    assert w._default_session_dir().endswith(str(w.Path(".karlon") / "wa_session"))    # home wins once it exists
