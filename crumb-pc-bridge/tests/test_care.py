"""The always-on pieces: tree killing, browser cleanup, lock, heartbeat,
restart rules. Run on any OS; the Windows-only calls are no-ops here."""

import asyncio
import os
import re
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from conftest import run_async

import crumb_care as care
import crumb_supervisor as sup


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:                                   # a zombie counts as gone
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split()[2] != "Z"
    except OSError:
        return True


def _spawner(tmp_path: Path) -> Path:
    """A child that starts a grandchild (like python → chrome) and prints."""
    script = tmp_path / "child.py"
    script.write_text(textwrap.dedent(f"""
        import subprocess, sys, time
        g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        open(r"{tmp_path / 'grandchild.pid'}", "w").write(str(g.pid))
        print("ready", flush=True)
        time.sleep(600)
    """))
    return script


def _wait_for(path: Path, timeout: float = 10) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and path.read_text():
            return path.read_text()
        time.sleep(0.05)
    raise AssertionError(f"{path} never appeared")


def test_stop_kills_the_whole_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(care, "STATE_DIR", tmp_path)
    e = sup.ManagedProcess(key="t", script=str(_spawner(tmp_path)))
    e.start(hidden=True)
    grandchild = int(_wait_for(tmp_path / "grandchild.pid"))
    assert _alive(grandchild)
    e.stop(grace=0)
    time.sleep(0.5)
    assert not e.alive()
    assert not _alive(grandchild), "the grandchild (the 'browser') must die with its parent"


def test_kill_tree_without_psutil_uses_the_process_group(tmp_path, monkeypatch):
    monkeypatch.setattr(care, "psutil", None)
    p = subprocess.Popen([sys.executable, str(_spawner(tmp_path))], **care.popen_kwargs(True))
    grandchild = int(_wait_for(tmp_path / "grandchild.pid"))
    care.kill_tree(p.pid, timeout=3)
    p.wait(timeout=5)
    time.sleep(0.3)
    assert not _alive(grandchild)


def test_clean_stop_file_lets_the_child_exit_itself(tmp_path, monkeypatch):
    monkeypatch.setattr(care, "STATE_DIR", tmp_path)
    script = tmp_path / "polite.py"
    script.write_text(textwrap.dedent("""
        import os, sys, time
        f = os.environ["CRUMB_CHILD_STOP_FILE"]
        print("up", flush=True)
        while not os.path.exists(f):
            time.sleep(0.1)
        print("closing cleanly", flush=True)
        sys.exit(0)
    """))
    e = sup.ManagedProcess(key="polite", script=str(script), clean_stop=True)
    e.start()
    time.sleep(0.5)
    t0 = time.time()
    e.stop(grace=10)
    assert e.proc.returncode == 0, "exited by itself, not killed"
    assert time.time() - t0 < 5
    assert not e.stop_file.exists()


def test_profile_cleanup_keeps_the_login(tmp_path):
    prof = tmp_path / "wa_session"
    for rel in ("Default/Cache/data_0", "Default/Code Cache/js/x", "GrShaderCache/a", "Default/GPUCache/b",
                "Default/IndexedDB/https_web.whatsapp.com_0.indexeddb.leveldb/000003.log",
                "Default/Local Storage/leveldb/CURRENT", "Default/Service Worker/Database/MANIFEST",
                "Default/Cookies", "Local State", "SingletonLock", "lockfile", "Crashpad/reports/r.dmp"):
        f = prof / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x" * 1000)
    freed = care.clean_profile(str(prof))
    assert freed >= 6000
    for gone in ("Default/Cache", "Default/Code Cache", "GrShaderCache", "Default/GPUCache", "SingletonLock",
                 "lockfile", "Crashpad/reports"):
        assert not (prof / gone).exists(), gone
    for kept in ("Default/IndexedDB", "Default/Local Storage", "Default/Service Worker/Database",
                 "Default/Cookies", "Local State"):
        assert (prof / kept).exists(), kept


def test_profile_locks_only_keeps_caches(tmp_path):
    prof = tmp_path / "p"
    (prof / "Default/Cache").mkdir(parents=True)
    (prof / "SingletonLock").write_text("x")
    care.clean_profile(str(prof), caches=False)
    assert not (prof / "SingletonLock").exists()
    assert (prof / "Default/Cache").exists()


def test_temp_cleanup_skips_recent_and_in_use(tmp_path):
    old = tmp_path / "playwright_chromiumdev_profile-old"
    busy = tmp_path / "playwright_chromiumdev_profile-busy"
    fresh = tmp_path / "playwright_chromiumdev_profile-fresh"
    other = tmp_path / "my_documents"
    for d in (old, busy, fresh, other):
        (d / "Default").mkdir(parents=True)
        (d / "Default" / "f").write_bytes(b"x" * 500)
    past = time.time() - 7200
    for d in (old, busy, other):
        for p in (d / "Default" / "f", d / "Default", d):
            os.utime(p, (past, past))
    freed = care.clean_temp(str(tmp_path), older_than_sec=3600,
                            in_use=f"chrome --user-data-dir={busy}")
    assert freed >= 500
    assert not old.exists()
    assert busy.exists() and fresh.exists() and other.exists()


def test_prune_logs(tmp_path):
    old, new = tmp_path / "crumb_20250101.log", tmp_path / "crumb_supervisor.log"
    shot = tmp_path / "wa_no_chats_1.png"
    for f in (old, new, shot):
        f.write_text("x")
    past = time.time() - 30 * 86400
    os.utime(old, (past, past))
    os.utime(shot, (past, past))
    assert care.prune_logs(str(tmp_path), days=14) == 2
    assert new.exists() and not old.exists() and not shot.exists()


def test_net_ok():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    assert care.net_ok(f"http://127.0.0.1:{port}")
    srv.close()
    assert not care.net_ok(f"http://127.0.0.1:{port}", timeout=1)


def test_single_instance_lock(tmp_path):
    a, b = care.InstanceLock(tmp_path / "x.lock"), care.InstanceLock(tmp_path / "x.lock")
    assert a.acquire()
    probe = subprocess.run([sys.executable, "-c", textwrap.dedent(f"""
        import sys; sys.path.insert(0, {str(Path(care.__file__).parent)!r})
        import crumb_care as c; from pathlib import Path
        print(c.InstanceLock(Path({str(tmp_path / 'x.lock')!r})).acquire())
    """)], capture_output=True, text=True)
    assert probe.stdout.strip() == "False", probe.stderr
    a.release()
    assert b.acquire()
    b.release()


def test_lock_is_freed_when_the_holder_is_killed(tmp_path):
    holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
        import sys, time; sys.path.insert(0, {str(Path(care.__file__).parent)!r})
        import crumb_care as c; from pathlib import Path
        lock = c.InstanceLock(Path({str(tmp_path / 'y.lock')!r})); assert lock.acquire(); print('held', flush=True)
        time.sleep(600)
    """)], stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "held"
    lock = care.InstanceLock(tmp_path / "y.lock")
    assert not lock.acquire()
    holder.kill()
    holder.wait()
    assert lock.acquire(), "a hard kill must never leave the lock stuck"
    lock.release()


def test_heartbeat_roundtrip_and_age(tmp_path):
    hb = tmp_path / "s.json"
    care.write_heartbeat({"pid": 123}, hb)
    beat = care.read_heartbeat(hb)
    assert beat["pid"] == 123 and care.heartbeat_age(beat) < 5
    assert care.heartbeat_age({}) == float("inf")
    assert care.read_heartbeat(tmp_path / "missing.json") == {}


def test_requests_are_taken_once(tmp_path):
    r = tmp_path / "stop.request"
    r.touch()
    assert care.take_request(r) and not care.take_request(r)


def test_rotating_log(tmp_path):
    log = care.RotatingLog(tmp_path / "k.log", max_mb=1, keep=2)
    log.max_bytes = 100
    for _ in range(10):
        log.write("x" * 60 + "\n")
    log.close()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["k.log", "k.log.1", "k.log.2"]


def test_child_python_prefers_console_interpreter(tmp_path, monkeypatch):
    (tmp_path / "pythonw.exe").write_text("")
    (tmp_path / "python.exe").write_text("")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "pythonw.exe"))
    assert care.child_python() == str(tmp_path / "python.exe")


class _Fake(sup.ManagedProcess):
    def __init__(self, key, **kw):
        super().__init__(key=key, script="x.py", **kw)
        self.up = True

    def alive(self):
        return self.up


@pytest.fixture()
def now():
    return 1_000_000.0


def _fake(key, now, up_h=1.0, silent_s=5, **kw):
    e = _Fake(key, **kw)
    e.proc = type("P", (), {"pid": hash(key) % 10000})()
    e.last_restart = now - up_h * 3600
    e.last_output = now - silent_s
    return e


def test_rules_memory_and_hang(now):
    wa = _fake("wa", now, max_mb=2500, silent_limit_sec=1800)
    inv = _fake("invoice", now, max_mb=1200, silent_s=99999)        # silent while idle: fine
    mem = {wa.proc.pid: 3100.0, inv.proc.pid: 90.0}
    out = sup.check_children([wa, inv], now, 0, 50, read_mem=mem.get)
    assert out == [("wa", "using 3100 MB (limit 2500 MB)")]
    wa2 = _fake("wa", now, max_mb=2500, silent_limit_sec=1800, silent_s=2000)
    assert "stuck" in sup.check_children([wa2], now, 0, 50, read_mem=lambda p: 500.0)[0][1]


def test_rules_outage_only_for_processes_that_sat_through_it(now):
    old = _fake("wa", now, up_h=2, after_outage=True)
    young = _fake("scraper", now, up_h=0.01, after_outage=True)    # started when the net came back
    inv = _fake("invoice", now, up_h=2)
    out = sup.check_children([old, young, inv], now, 600, 40, read_mem=lambda p: 100.0)
    assert [k for k, _ in out] == ["wa"]
    assert sup.check_children([old], now, 30, 40, read_mem=lambda p: 100.0) == []   # a blip


def test_rules_one_recycle_at_a_time_and_low_pc_memory(now):
    a, b = _fake("wa", now, up_h=30), _fake("invoice", now, up_h=30)
    out = sup.check_children([a, b], now, 0, 40, read_mem=lambda p: 100.0)
    assert len(out) == 1 and "fresh start" in out[0][1]
    c, d = _fake("wa", now, max_mb=2500), _fake("scraper", now, max_mb=3000)
    mem = {c.proc.pid: 1800.0, d.proc.pid: 900.0}
    low = {"at": 0.0}
    out = sup.check_children([c, d], now, 0, 95, read_mem=mem.get, low_mem=low)
    assert out[0][0] == "wa" and "PC memory" in out[0][1]
    assert sup.check_children([_fake("wa", now, max_mb=2500)], now + 60, 0, 95,
                              read_mem=lambda p: 1800.0, low_mem=low) == []        # cooldown


def test_full_pc_from_other_programs_never_causes_a_restart_loop(now):
    """The user's log: PC at 97-99% (browser tabs etc.), WhatsApp just logged
    in (1.2 GB) and the scraper just started (0.8 GB). Restarting them freed
    nothing lasting and killed WhatsApp mid-sync every minute; now neither is
    touched (too young, and well under their own limits)."""
    low = {"at": 0.0}
    wa = _fake("wa", now, up_h=1 / 60, max_mb=2500)
    sc = _fake("scraper", now, up_h=0.005, max_mb=3000)
    mem = {wa.proc.pid: 1184.0, sc.proc.pid: 789.0}
    for t in range(0, 3600, 15):                              # an hour of monitor ticks
        wa.last_restart = sc.last_restart = now - 60          # whatever their age, they stay
        assert sup.check_children([wa, sc], now + t, 0, 98, read_mem=mem.get, low_mem=low) == []
    old_wa = _fake("wa", now, up_h=2, max_mb=2500)            # an old, small WhatsApp: still not Crumb's fault
    assert sup.check_children([old_wa], now, 0, 98, read_mem=lambda p: 900.0, low_mem=low) == []


def test_supervisor_end_to_end(tmp_path):
    """The real supervisor, fake children: starts them, refuses a second
    copy, holds restarts while the network is down, and --stop takes the
    whole tree down (grandchildren included)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(50)
    port = srv.getsockname()[1]
    child = textwrap.dedent("""
        import os, subprocess, sys, time
        g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        open(os.path.basename(sys.argv[0]) + ".gpid", "a").write(f"{g.pid}\\n")
        stop = os.environ["CRUMB_CHILD_STOP_FILE"]
        while not os.path.exists(stop):
            print("tick", flush=True)
            time.sleep(0.5)
    """)
    (tmp_path / "crumb_whatsapp.py").write_text(child)
    env = {**os.environ, "CRUMB_STATE_DIR": str(tmp_path / "state"), "CRUMB_URL": f"http://127.0.0.1:{port}",
           "CRUMB_LOG_DIR": str(tmp_path / "logs"), "CRUMB_MONITOR_SEC": "1",
           "CRUMB_WA_SESSION_DIR": str(tmp_path / "wa"), "CRUMB_KEEP_AWAKE": "0"}
    script = str(Path(sup.__file__))
    run = lambda *a: subprocess.Popen([sys.executable, script, *a], cwd=tmp_path, env=env,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    main = run("--service")
    hb = tmp_path / "state" / "supervisor.json"
    deadline = time.time() + 20
    beat = {}
    while time.time() < deadline:
        beat = care.read_heartbeat(hb)
        kids = beat.get("children") or {}
        if kids and all(c.get("running") for c in kids.values()):
            break
        time.sleep(0.3)
    assert all(c["running"] for c in beat["children"].values()), beat

    second = run()
    out, _ = second.communicate(timeout=20)
    assert "already running" in out

    first_wa = beat["children"]["wa"]["pid"]
    srv.close()                                   # the network goes
    os.kill(first_wa, 9)                          # and wa dies with it
    time.sleep(4)
    beat = care.read_heartbeat(hb)
    assert beat["network"]["ok"] is False
    assert beat["children"]["wa"]["running"] is False, "no restart while offline"

    srv = socket.socket()                         # the network is back
    srv.bind(("127.0.0.1", port))
    srv.listen(50)
    deadline = time.time() + 30
    while time.time() < deadline:
        beat = care.read_heartbeat(hb)
        if beat["children"]["wa"]["running"]:
            break
        time.sleep(0.3)
    assert beat["children"]["wa"]["running"] and beat["children"]["wa"]["pid"] != first_wa

    subprocess.run([sys.executable, script, "--stop"], cwd=tmp_path, env=env, check=True, capture_output=True)
    main.communicate(timeout=60)
    assert main.returncode == 0
    time.sleep(0.5)
    gpids = [int(x) for f in tmp_path.glob("*.gpid") for x in f.read_text().split()]
    assert len(gpids) >= 2
    assert not any(_alive(g) for g in gpids), "every grandchild ('browser') is gone"
    assert care.read_heartbeat(hb)["state"] == "stopped"
    log = (tmp_path / "logs" / "crumb_supervisor.log").read_text()
    assert "[wa] tick" in log and "no network" in log
    srv.close()


def test_powershell_scripts_are_plain_ascii():
    """Windows PowerShell 5.1 reads a script without a BOM as ANSI: a UTF-8
    dash becomes bytes it parses as a closing quote and the script breaks."""
    here = Path(__file__).resolve().parent.parent
    scripts = sorted(here.glob("*.ps1"))
    assert {p.name for p in scripts} >= {"install_crumb.ps1", "uninstall_crumb.ps1", "crumb_status.ps1"}
    for p in scripts:
        data = p.read_bytes()
        assert all(b < 128 for b in data), f"{p.name} has non-ASCII bytes"


def test_only_the_whatsapp_bridge():
    assert list(sup.ALL_PROCESSES) == ["wa"] and sup.ALL_PROCESSES["wa"].script == "crumb_whatsapp.py"


def test_headless_once_the_login_is_saved(tmp_path, monkeypatch):
    """WhatsApp runs headless once its login is saved and needs a visible
    window (a desktop) until then."""
    wa = sup.ALL_PROCESSES["wa"]
    assert "--headless" not in wa.args             # the script decides from its saved login
    monkeypatch.setattr(wa, "headless_setting", "auto")
    monkeypatch.setattr(wa, "login_saved", lambda: False)
    assert wa.headless() is False and wa.needs_desktop()
    monkeypatch.setattr(wa, "login_saved", lambda: True)
    assert wa.headless() is True and not wa.needs_desktop()
    monkeypatch.setattr(wa, "headless_setting", "0")            # forced visible
    assert wa.needs_desktop()


def test_no_desktop_never_runs_site_browsers(monkeypatch):
    """Before sign-in Chrome can't open its saved logins (DPAPI): WhatsApp and
    the scraper wait for the signed-in copy even with a saved login; the
    invoice worker (no login) runs."""
    monkeypatch.setattr(sup, "_no_desktop", True)
    for saved in (False, True):
        wa = _Fake("wa")
        wa.login_saved, wa.headless_setting = (lambda s=saved: s), "auto"
        seen = []
        monkeypatch.setattr(sup, "_sleep", lambda s: seen.append(set(sup._waiting)) or sup._stop.set())
        try:
            assert sup.wait_for_desktop(wa) is False and seen == [{"wa"}]
        finally:
            sup._stop.clear()
            sup._waiting.clear()
    assert sup.wait_for_desktop(_Fake("invoice")) is True            # no login: runs anywhere
    monkeypatch.setattr(sup, "_no_desktop", False)
    wa = _Fake("wa")
    wa.login_saved, wa.headless_setting = (lambda: True), "auto"
    assert sup.wait_for_desktop(wa) is True and wa.headless() is True   # signed in: headless


def test_headless_choice():
    from local_config import headless_choice
    assert headless_choice("auto", True) and not headless_choice("auto", False)
    assert headless_choice("1", False) and not headless_choice("0", True)


def test_headless_whatsapp_logged_out_forgets_the_login(tmp_path, monkeypatch):
    import crumb_whatsapp as w
    monkeypatch.setattr(w, "SESSION_DIR", str(tmp_path))
    w.mark_logged_in(True)
    assert (tmp_path / "crumb_logged_in").exists() and w.wants_headless()
    monkeypatch.setattr(w, "whatsapp_state", lambda page: ("qr", "Scan the QR code"))
    assert w.wait_for_whatsapp(None, headless=True) is False
    assert not (tmp_path / "crumb_logged_in").exists(), "next start opens a visible window"
    assert not w.wants_headless()


def test_installer_tasks():
    """Only tasks in the signed-in session: a browser started before sign-in
    can't read Chrome's saved logins and spoils the profile."""
    here = Path(__file__).resolve().parent.parent
    text = (here / "install_crumb.ps1").read_text()
    assert "LogonType S4U" not in text and "-AtStartup" not in text
    assert "LogonType Interactive" in text and "Stop-ScheduledTask" in text
    login = (here / "crumb_login.py").read_text()
    assert "login.hold" in login and "login.hold" in (here / "crumb_supervisor.py").read_text()


def test_separate_from_karlon():
    """Its own folder, profile, task and environment names: it runs next to
    Karlon's bridge without either touching the other."""
    import crumb_care as c
    import local_config as lc
    here = Path(__file__).resolve().parent.parent
    assert c.STATE_DIR.name == ".crumb" and ".crumb" in lc.wa_session_dir()
    assert lc.wa_login_marker().name == "crumb_logged_in"
    for f in here.glob("*.py"):
        code = f.read_text(encoding="utf-8")
        assert "karlon_" not in code and "KARLON_" not in code, f.name      # Karlon's modules and settings
        assert not re.search(r'environ(?:\.get)?[\[(]"(?!CRUMB_|TEMP"|TMPDIR")', code), f.name
    uninstall = (here / "uninstall_crumb.ps1").read_text()
    assert "ms-playwright" not in uninstall and "Software\\Karlon" in uninstall
    assert "TaskPath  = '\\Crumb\\'" in (here / "install_crumb.ps1").read_text()
