"""karlon_supervisor.py — one console, three managed processes, built to run
unattended for weeks.

Replaces opening wa_bridge.py, invoice_worker.py, and
airbnb_parallel_system.py in three separate PowerShell tabs by hand.
Spawns each as its own subprocess (they are NOT imported — wa_bridge.py
uses Playwright's *sync* API and airbnb_parallel_system.py uses
Playwright's *async* API; the two cannot share a thread/event loop, so
each keeps its own process and its own Python interpreter, same as
running them separately today), tags and forwards each one's output to
this single window, and restarts any one of them on its own if it exits
unexpectedly — a scraper login timeout should never take down WhatsApp
sync or invoice generation.

Always-on behaviour (install_karlon.ps1 starts this with --service at
sign-in, on the desktop: WhatsApp Web and Airbnb need a visible browser):

* One instance per PC (an OS file lock). A second start exits at once,
  unless the running one stopped answering (heartbeat older than 3 min) —
  then it's killed and replaced. --takeover replaces an instance running on
  another Windows session (one with no desktop never runs the browsers).
* Each process runs with its browsers in a Windows job object: a restart,
  or the supervisor dying, kills the whole tree. No orphan chrome.exe.
* Waits for the network before starting anything, never crash-loops while
  the Wi-Fi is off, and restarts the browser processes once the network is
  back after a real outage.
* Restarts a process that uses too much memory, prints nothing for too long
  (hung), or has been up KARLON_RECYCLE_HOURS; restarts the biggest one if
  the whole PC runs low on memory.
* Cleans the WhatsApp browser profile (stale locks, caches) before each
  start, and stale Playwright temp profiles and old logs every 30 minutes.
* Keeps Windows from idle-sleeping while it runs.
* Writes ~/.karlon/supervisor.json every 15 s (what karlon_status.ps1 shows).

RUN:
    python karlon_supervisor.py                  # all three, in this console
    python karlon_supervisor.py --only wa,invoice # a subset, for debugging
    python karlon_supervisor.py --status          # what the running one is doing
    python karlon_supervisor.py --restart         # ask it to restart its processes
    python karlon_supervisor.py --stop            # ask it to stop
    KARLON_URL=http://127.0.0.1:8000 python karlon_supervisor.py

Ctrl+C once, here, stops all three cleanly.
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import karlon_care as care
from local_config import (KARLON_URL, LOG_DIR, KARLON_WA_MAX_MB, KARLON_SCRAPER_MAX_MB, KARLON_INVOICE_MAX_MB,
                          KARLON_RECYCLE_HOURS, KARLON_OUTAGE_RESTART_SEC, KARLON_SYS_MEM_PCT, KARLON_KEEP_AWAKE,
                          KARLON_WA_HEADLESS, KARLON_AIRBNB_HEADLESS, AIRBNB_AUTH_STATE_FILE, headless_choice,
                          wa_session_dir, wa_login_marker, print_active_config)

# One-for-one restart with exponential backoff: 5s, 10s, 20s ... capped at
# MAX_BACKOFF_SEC. A run that stayed up at least STABLE_RUN_SEC counts as
# healthy and resets the ladder, so a process that crashes once a day always
# restarts in 5s, while one that dies on launch backs off to a slow retry
# instead of hammering the server (or Airbnb, or WhatsApp Web) every 30s.
RESTART_BACKOFF_SEC = 5
MAX_BACKOFF_SEC = 300
STABLE_RUN_SEC = 60

MONITOR_SEC = max(1, int(os.environ.get("KARLON_MONITOR_SEC", "15") or 15))   # memory / hang / network / heartbeat
HOUSEKEEPING_SEC = 1800       # orphan browsers, temp profiles, old logs
PROFILE_CLEAN_SEC = 86400     # browser caches: at most once a day
CLEAN_STOP_GRACE_SEC = 45     # how long a process gets to close its browser itself
NET_WAIT_MAX_SEC = 60         # longest pause between network checks while it's down


def _wa_login_saved() -> bool:
    return wa_login_marker(wa_session_dir()).exists()


def _airbnb_login_saved() -> bool:
    return Path(AIRBNB_AUTH_STATE_FILE).exists()


@dataclass
class ManagedProcess:
    key: str            # short tag used in log prefixes, e.g. "wa"
    script: str         # filename to run with the current interpreter
    args: list[str] = field(default_factory=list)
    # Extra env vars for this process only (merged over os.environ). Used to
    # tell airbnb_parallel_system.py it's running unattended — see
    # prompt_for_search_queries()'s KARLON_SCRAPER_NONINTERACTIVE check.
    # Without this, that process's input() prompt inherits this console's
    # stdin (shared with every other managed process here) and can sit
    # waiting on a prompt nobody can reliably type into, hanging the scrape
    # indefinitely without ever exiting — so the restart/backoff logic below
    # never even notices anything is wrong.
    env: dict[str, str] = field(default_factory=dict)
    max_mb: int = 0               # restart above this much memory, browsers included (0 = no cap)
    silent_limit_sec: int = 0     # restart after this long without a line of output (0 = never)
    after_outage: bool = False    # restart once the network is back after an outage
    profile_dir: str = ""         # browser profile cleaned before each start
    clean_stop: bool = False      # understands KARLON_CHILD_STOP_FILE (closes its browser itself)
    # A browser that logs in to a site: headless once its login is saved
    # (karlon_login.py), a visible window - which needs a desktop - until then.
    headless_setting: str = ""
    login_saved: object = None    # () -> bool
    proc: subprocess.Popen | None = None
    job: care.JobObject | None = None
    last_restart: float = 0.0
    last_output: float = 0.0
    restart_count: int = 0        # lifetime total, for the log line
    consecutive_fast_failures: int = 0
    restart_reason: str = ""      # set by the monitor: stop this run and start again
    mem_mb: float | None = None
    last_exit: int | None = None
    last_profile_clean: float = 0.0
    # The watch thread and shutdown can both stop a process; one at a time.
    _stopping: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def headless(self) -> bool | None:
        """True/False for a site browser, None for a process with no login."""
        if self.login_saved is None:
            return None
        return headless_choice(self.headless_setting, self.login_saved())

    def needs_desktop(self) -> bool:
        return self.headless() is False

    @property
    def stop_file(self) -> Path:
        return care.STATE_DIR / f"stop.{self.key}"

    def start(self, hidden: bool = False) -> None:
        # Force every child to *emit* UTF-8 (PYTHONIOENCODING / PYTHONUTF8) and
        # decode its pipe as UTF-8 here. Without both, Windows defaults the
        # pipe to cp1252: the first emoji/✓/✗ in a child's print() either
        # crashed the child ("'charmap' codec can't encode character") or
        # crashed our _reader_thread ("'charmap' codec can't decode byte 0x8f")
        # — which is exactly the traceback that was showing in PowerShell.
        proc_env = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "KARLON_CHILD_STOP_FILE": str(self.stop_file),
            **self.env,
        }
        _unlink(self.stop_file)
        self.proc = subprocess.Popen(
            [care.child_python(), "-u", self.script, *self.args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=proc_env,
            **care.popen_kwargs(hidden),
        )
        # Its own job: closing it later kills the python AND every browser it
        # started; if this supervisor dies, Windows closes it for us.
        self.job = care.JobObject()
        self.job.add(self.proc)
        self.last_restart = self.last_output = time.time()
        self.restart_reason = ""
        self.mem_mb = None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, grace: float = CLEAN_STOP_GRACE_SEC) -> None:
        """Stop this run and everything it started. A process that knows the
        stop file gets `grace` seconds to close its browser properly (the
        WhatsApp profile is saved cleanly); then, or for the others at once,
        the whole tree is killed."""
        with self._stopping:
            self._stop(grace)

    def _stop(self, grace: float) -> None:
        if self.proc is None:
            return
        if self.alive() and self.clean_stop and grace > 0:
            try:
                self.stop_file.parent.mkdir(parents=True, exist_ok=True)
                self.stop_file.touch()
                self.proc.wait(timeout=grace)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if self.alive():
            if self.job and self.job.handle:
                self.job.kill()
            else:
                care.kill_tree(self.proc.pid)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            care.kill_tree(self.proc.pid, timeout=3)
        self.reap()
        _unlink(self.stop_file)

    def reap(self) -> None:
        """After the process has exited, by itself or not: whatever it left
        running (a browser it didn't close) goes too."""
        with self._stopping:
            self._reap()

    def _reap(self) -> None:
        if self.job:
            self.job.close()          # anything still in the job dies here
            self.job = None
        if self.proc is not None:
            care.kill_group(self.proc.pid)


# The three processes this supervisor knows about. Add/remove entries here
# (not by hand-editing the watch loop) if the set of managed scripts ever
# changes. wa prints a line every poll, so 30 min of silence means it's stuck;
# the invoice worker is silent while idle, so it has no silence limit.
ALL_PROCESSES: dict[str, ManagedProcess] = {
    "wa":      ManagedProcess(key="wa", script="wa_bridge.py", max_mb=KARLON_WA_MAX_MB,
                              silent_limit_sec=1800, after_outage=True,
                              profile_dir=wa_session_dir(), clean_stop=True,
                              headless_setting=KARLON_WA_HEADLESS, login_saved=_wa_login_saved),
    "invoice": ManagedProcess(key="invoice", script="invoice_worker.py", max_mb=KARLON_INVOICE_MAX_MB),
    "scraper": ManagedProcess(
        key="scraper", script="airbnb_parallel_system.py",
        env={"KARLON_SCRAPER_NONINTERACTIVE": "1"},
        max_mb=KARLON_SCRAPER_MAX_MB, silent_limit_sec=3600, after_outage=True,
        headless_setting=KARLON_AIRBNB_HEADLESS, login_saved=_airbnb_login_saved,
    ),
}

_stop = threading.Event()
_out_lock = threading.Lock()
_net = {"ok": True, "down_since": None, "checked": 0.0}
_hidden = False
_mode = "console"
_no_desktop = False           # Windows session 0: started before anyone signed in
_waiting: set[str] = set()    # processes held back until a desktop is there


def compute_backoff(consecutive_fast_failures: int) -> int:
    """Seconds to wait before restart number `consecutive_fast_failures + 1`."""
    return min(MAX_BACKOFF_SEC, RESTART_BACKOFF_SEC * (2 ** consecutive_fast_failures))


def _unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def log(msg: str) -> None:
    """Timestamped line to the console (when there is one) and the log."""
    line = f"{datetime.now():%m-%d %H:%M:%S} {msg}"
    with _out_lock:
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode("ascii"), flush=True)
        except (OSError, ValueError, AttributeError):
            pass


class _Tee:
    """stdout/stderr → the rotating log file (+ the console if there is one;
    pythonw.exe, which the scheduled task runs, has none)."""

    def __init__(self, logfile: care.RotatingLog, console) -> None:
        self.logfile, self.console = logfile, console

    def write(self, text: str) -> int:
        self.logfile.write(text)
        if self.console is not None:
            try:
                self.console.write(text)
            except (OSError, ValueError, UnicodeEncodeError):
                pass
        return len(text)

    def flush(self) -> None:
        self.logfile.flush()
        if self.console is not None:
            try:
                self.console.flush()
            except (OSError, ValueError):
                pass


def _reader_thread(proc_entry: ManagedProcess) -> None:
    """Forwards one child's stdout to this console (and the combined log
    file), prefixed with its tag, until the child exits or we're stopping.
    Wrapped so a single unprintable line can never kill the reader (and with
    it the restart logic) again. Every line also counts as a sign of life."""
    proc = proc_entry.proc
    assert proc is not None
    try:
        for line in proc.stdout:
            proc_entry.last_output = time.time()
            log(f"[{proc_entry.key}] {line.rstrip()}")
    except Exception as e:
        log(f"[supervisor] reader for {proc_entry.key} stopped: {e!r}")


def _sleep(seconds: float) -> bool:
    """Sleep, waking at once if we're stopping. False if stopping."""
    return not _stop.wait(seconds)


def wait_for_network() -> bool:
    """Block until the server can be reached (DNS + TCP), checking with a
    growing pause (5 s … 60 s) so a PC offline all night costs nothing.
    False if we're stopping."""
    pause, said = 5, False
    while not _stop.is_set():
        if _net["ok"] and time.time() - _net["checked"] < MONITOR_SEC * 2:
            return True
        if care.net_ok(KARLON_URL):
            _set_net(True)
            if said:
                log("[supervisor] network is back")
            return True
        _set_net(False)
        if not said:
            log(f"[supervisor] no network (can't reach {KARLON_URL}) — waiting; nothing restarts until it's back")
            said = True
        if not _sleep(pause):
            return False
        pause = min(NET_WAIT_MAX_SEC, pause * 2)
    return False


def wait_for_desktop(entry: ManagedProcess) -> bool:
    """With no desktop (started at power-on, nobody signed in), a browser
    that still needs a person to log in can't show its window: hold it until
    its login is saved (karlon_login.py) or the signed-in copy takes over.
    Processes with a saved login run headless and never wait. False if
    we're stopping."""
    said = False
    while _no_desktop and entry.needs_desktop() and not _stop.is_set():
        if not said:
            log(f"[supervisor] {entry.key} has no saved login and there's no desktop to show a login window: "
                f"it starts at sign-in (or after: python karlon_login.py)")
            said = True
            _waiting.add(entry.key)
        _sleep(30)
    _waiting.discard(entry.key)
    return not _stop.is_set()


def _set_net(ok: bool) -> None:
    """Record a network check (for wait_for_network and the heartbeat)."""
    now = time.time()
    _net["checked"], _net["ok"] = now, ok
    if ok:
        _net["down_since"] = None
    elif _net["down_since"] is None:
        _net["down_since"] = now


def _prepare_browser(entry: ManagedProcess) -> None:
    """Before a browser process starts: no leftover browser may still hold
    its profile, stale lock files go, and (once a day) the caches."""
    if not entry.profile_dir:
        return
    killed = care.sweep_orphan_browsers(entry.profile_dir, keep=set(), profile_only=True)
    if killed:
        log(f"[supervisor] closed {killed} leftover browser process(es) still holding the {entry.key} profile")
        time.sleep(2)
    caches = time.time() - entry.last_profile_clean > PROFILE_CLEAN_SEC
    freed = care.clean_profile(entry.profile_dir, caches=caches)
    if caches:
        entry.last_profile_clean = time.time()
    if freed > 2**20:
        log(f"[supervisor] cleaned {entry.key} browser profile: {freed / 2**20:.0f} MB freed (login kept)")


def _watch(proc_entry: ManagedProcess) -> None:
    """Owns one process's whole lifecycle: start, forward output, and on
    exit — unless we're shutting down — restart it after a backoff that
    grows if it's crash-looping. A restart the monitor asked for (memory,
    hang, recycle, network back) is planned: no backoff, no failure count."""
    while not _stop.is_set():
        if not wait_for_desktop(proc_entry) or not wait_for_network():
            return
        try:
            _prepare_browser(proc_entry)
            proc_entry.start(hidden=_hidden)
        except Exception as e:
            log(f"[supervisor] ! could not start {proc_entry.key}: {e!r}")
            if not _sleep(compute_backoff(proc_entry.consecutive_fast_failures)):
                return
            proc_entry.consecutive_fast_failures += 1
            continue
        log(f"[supervisor] started {proc_entry.key} ({proc_entry.script}), pid={proc_entry.proc.pid}")
        reader = threading.Thread(target=_reader_thread, args=(proc_entry,), daemon=True)
        reader.start()

        while proc_entry.alive() and not _stop.is_set() and not proc_entry.restart_reason:
            time.sleep(1)
        planned = proc_entry.restart_reason
        if proc_entry.alive():
            if planned:
                log(f"[supervisor] restarting {proc_entry.key}: {planned}")
            proc_entry.stop()
        exit_code = proc_entry.proc.returncode
        proc_entry.last_exit = exit_code
        proc_entry.reap()
        reader.join(timeout=2)

        if _stop.is_set():
            log(f"[supervisor] {proc_entry.key} exited (code {exit_code}) during shutdown — not restarting")
            return

        proc_entry.restart_count += 1
        if planned:
            proc_entry.consecutive_fast_failures = 0
            _sleep(2)
            continue
        if not care.net_ok(KARLON_URL):
            # It died because the network went: no failure, no backoff —
            # wait_for_network() holds the restart until it's back.
            _set_net(False)
            log(f"[supervisor] {proc_entry.key} exited (code {exit_code}) while the network is down")
            continue

        uptime = time.time() - proc_entry.last_restart
        if uptime >= STABLE_RUN_SEC:
            proc_entry.consecutive_fast_failures = 0
        backoff = compute_backoff(proc_entry.consecutive_fast_failures)
        proc_entry.consecutive_fast_failures += 1

        log(
            f"[supervisor] ! {proc_entry.key} exited unexpectedly (code {exit_code}). "
            f"Restarting in {backoff}s (restart #{proc_entry.restart_count})"
        )
        if not _sleep(backoff):
            return


def check_children(entries: list[ManagedProcess], now: float, net_back_after: float,
                   sys_mem_pct: float | None, read_mem=care.tree_memory_mb) -> list[tuple[str, str]]:
    """Decide which running processes need a fresh start, and why. Pure
    except for read_mem, so the rules are testable. At most one recycle per
    call, so processes don't all restart in the same minute."""
    out: list[tuple[str, str]] = []
    recycled = False
    running = [e for e in entries if e.alive() and not e.restart_reason]
    for e in running:
        e.mem_mb = read_mem(e.proc.pid)
        up = now - e.last_restart
        if (net_back_after and e.after_outage and net_back_after >= KARLON_OUTAGE_RESTART_SEC
                and e.last_restart < now - net_back_after):            # it sat through the outage
            out.append((e.key, f"network back after {net_back_after / 60:.0f} min offline"))
        elif e.max_mb and e.mem_mb and e.mem_mb > e.max_mb:
            out.append((e.key, f"using {e.mem_mb:.0f} MB (limit {e.max_mb} MB)"))
        elif e.silent_limit_sec and now - e.last_output > e.silent_limit_sec:
            out.append((e.key, f"no output for {(now - e.last_output) / 60:.0f} min (stuck)"))
        elif KARLON_RECYCLE_HOURS and up > KARLON_RECYCLE_HOURS * 3600 and not recycled:
            out.append((e.key, f"up {up / 3600:.0f} h — routine fresh start"))
            recycled = True
    if sys_mem_pct and sys_mem_pct >= KARLON_SYS_MEM_PCT and not out:
        big = max((e for e in running if e.mem_mb), key=lambda e: e.mem_mb, default=None)
        if big:
            out.append((big.key, f"PC memory at {sys_mem_pct:.0f}% — freeing {big.mem_mb:.0f} MB"))
    return out


def _heartbeat(selected: dict[str, ManagedProcess], started: float) -> None:
    now = time.time()
    care.write_heartbeat({
        "pid": os.getpid(), "session": care.session_id(), "mode": _mode, "host": socket.gethostname(),
        "started": started, "karlon_url": KARLON_URL, "log": str(Path(LOG_DIR).resolve() / "karlon_supervisor.log"),
        "network": {"ok": _net["ok"], "down_since": _net["down_since"]},
        "system_memory_pct": care.system_memory_percent(),
        "waiting_for_desktop": sorted(_waiting),
        "children": {
            k: {"pid": e.proc.pid if e.alive() else None, "running": e.alive(),
                "uptime_s": round(now - e.last_restart) if e.alive() else 0,
                "memory_mb": e.mem_mb, "restarts": e.restart_count, "last_exit": e.last_exit,
                "silent_s": round(now - e.last_output) if e.alive() else None,
                "headless": e.headless(), "waiting_for_desktop": k in _waiting}
            for k, e in selected.items()
        },
    })


def _housekeeping(selected: dict[str, ManagedProcess]) -> None:
    freed = care.clean_temp()
    removed = care.prune_logs(LOG_DIR)
    t0 = time.time()                  # a browser starting after this is never swept
    keep = {pid for e in selected.values() if e.alive() for pid in care.tree_pids(e.proc.pid)}
    orphans = care.sweep_orphan_browsers(wa_session_dir(), keep, started_before=t0)
    if freed > 2**20 or removed or orphans:
        log(f"[supervisor] housekeeping: {freed / 2**20:.0f} MB browser temp freed, "
            f"{removed} old log file(s) removed, {orphans} orphan browser(s) closed")


def _monitor(selected: dict[str, ManagedProcess], started: float) -> None:
    """Every MONITOR_SEC: network, memory, hangs, requests, heartbeat."""
    last_house = 0.0
    outage_start = None           # its own record: the watch threads also update _net
    while not _stop.is_set():
        try:
            now = time.time()
            if care.take_request(care.STOP_REQUEST):
                log("[supervisor] stop requested — stopping everything")
                _stop.set()
                break
            if care.take_request(care.RESTART_REQUEST):
                for e in selected.values():
                    e.restart_reason = "restart requested"
            ok = care.net_ok(KARLON_URL)
            _set_net(ok)
            back_after = 0.0
            if not ok and outage_start is None:
                outage_start = now
                log("[supervisor] network lost — processes keep their state and retry; restarts wait for it")
            elif ok and outage_start is not None:
                back_after, outage_start = now - outage_start, None
                log(f"[supervisor] network back after {back_after:.0f}s")
                for e in selected.values():
                    e.consecutive_fast_failures = 0
            for key, why in check_children(list(selected.values()), now, back_after, care.system_memory_percent()):
                selected[key].restart_reason = why
            if now - last_house > HOUSEKEEPING_SEC:
                last_house = now
                _housekeeping(selected)
            _heartbeat(selected, started)
        except Exception as e:
            log(f"[supervisor] monitor error (continuing): {e!r}")
        _sleep(MONITOR_SEC)


def claim_instance(takeover: bool) -> care.InstanceLock | None:
    """The single-instance lock. Replaces a running supervisor that stopped
    answering; with takeover, asks one on another Windows session (the hidden
    one with no desktop) to hand over. None = another one is running fine."""
    lock = care.InstanceLock()
    if lock.acquire():
        return lock
    beat = care.read_heartbeat()
    for _ in range(30):               # one starting at the same moment: give it 15 s to write its first heartbeat
        if care.heartbeat_age(beat) < care.HEARTBEAT_STALE_SEC and beat.get("state") != "stopped":
            break
        time.sleep(0.5)
        beat = care.read_heartbeat()
    pid, age = beat.get("pid"), care.heartbeat_age(beat)
    if age > care.HEARTBEAT_STALE_SEC:
        log(f"[supervisor] the running supervisor (pid {pid}) stopped answering "
            f"({age:.0f}s since its last heartbeat) — replacing it")
        if pid and care.is_supervisor(pid):
            care.kill_tree(pid)
    elif takeover and beat.get("session") != care.session_id() and beat.get("waiting_for_desktop"):
        log(f"[supervisor] taking over from pid {pid} (Windows session {beat.get('session')}): "
            f"{', '.join(beat['waiting_for_desktop'])} need(s) this desktop for a login window")
        care.STOP_REQUEST.parent.mkdir(parents=True, exist_ok=True)
        care.STOP_REQUEST.touch()
    else:
        log(f"[supervisor] already running (pid {pid}, heartbeat {age:.0f}s ago) — nothing to do. "
            f"See it with: python karlon_supervisor.py --status")
        return None
    deadline = time.time() + 120
    while time.time() < deadline:
        if lock.acquire():
            return lock
        if time.time() > deadline - 30 and pid and care.is_supervisor(pid):
            care.kill_tree(pid)
        time.sleep(1)
    log("[supervisor] could not take over the running instance")
    return None


def _force_utf8_console() -> None:
    """Make this console itself UTF-8 so the forwarded lines print cleanly
    (PowerShell defaults to cp1252 for Python's stdout)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _print_status() -> None:
    beat = care.read_heartbeat()
    if not beat:
        print("Karlon supervisor: no heartbeat yet (never started on this PC?)")
        return
    age = care.heartbeat_age(beat)
    state = "RUNNING" if age < care.HEARTBEAT_STALE_SEC and beat.get("state") != "stopped" else "NOT RUNNING"
    print(f"Karlon supervisor: {state}  pid {beat.get('pid')}  mode {beat.get('mode')}  "
          f"session {beat.get('session')}  heartbeat {age:.0f}s ago")
    net = beat.get("network") or {}
    print(f"  network: {'ok' if net.get('ok') else 'DOWN'}   PC memory: {beat.get('system_memory_pct')}%")
    for k, c in (beat.get("children") or {}).items():
        print(f"  {k:8} {'up' if c.get('running') else 'down':5} pid {c.get('pid')}  "
              f"up {int(c.get('uptime_s') or 0) // 60} min  {c.get('memory_mb')} MB  restarts {c.get('restarts')}")
    print(f"  log: {beat.get('log')}")


def main() -> None:
    global _hidden, _mode, _no_desktop
    _force_utf8_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--only",
        help="comma-separated subset to run, e.g. 'wa,invoice' (default: all three)",
    )
    ap.add_argument("--service", action="store_true",
                    help="unattended: no console windows for the processes, log to file only (the scheduled task)")
    ap.add_argument("--takeover", action="store_true",
                    help="take over from an instance on another Windows session (e.g. one with no desktop)")
    ap.add_argument("--status", action="store_true", help="show what the running supervisor is doing")
    ap.add_argument("--stop", action="store_true", help="ask the running supervisor to stop")
    ap.add_argument("--restart", action="store_true", help="ask the running supervisor to restart its processes")
    args = ap.parse_args()

    if args.status:
        _print_status()
        return
    if args.stop or args.restart:
        req = care.STOP_REQUEST if args.stop else care.RESTART_REQUEST
        req.parent.mkdir(parents=True, exist_ok=True)
        req.touch()
        print(f"requested {'stop' if args.stop else 'restart'} — the supervisor acts on it within {MONITOR_SEC}s")
        return

    selected_keys = args.only.split(",") if args.only else list(ALL_PROCESSES.keys())
    unknown = [k for k in selected_keys if k not in ALL_PROCESSES]
    if unknown:
        print(f"[supervisor] unknown key(s) {unknown} — choose from {list(ALL_PROCESSES.keys())}")
        return
    selected = {k: ALL_PROCESSES[k] for k in selected_keys}

    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
    log_path = Path(LOG_DIR) / "karlon_supervisor.log"
    logfile = care.RotatingLog(log_path)
    sys.stdout = _Tee(logfile, sys.stdout)
    sys.stderr = _Tee(logfile, sys.stderr)
    _hidden = args.service
    _mode = "service" if args.service else "console"

    hold = care.STATE_DIR / "login.hold"
    if hold.exists() and time.time() - hold.stat().st_mtime < 1800:
        log("[supervisor] karlon_login.py is saving the logins - not starting until it's done")
        return
    _no_desktop = care.IS_WINDOWS and care.session_id() == 0

    lock = claim_instance(args.takeover)
    if lock is None:
        return
    for req in (care.STOP_REQUEST, care.RESTART_REQUEST):
        _unlink(req)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: _stop.set())
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda *_: _stop.set())

    print_active_config()
    awake = KARLON_KEEP_AWAKE and care.keep_awake(True)
    started = time.time()
    log(f"[supervisor] managing: {', '.join(selected)}  |  KARLON_URL={KARLON_URL}  |  mode {_mode}  |  "
        f"session {care.session_id()}  |  keep-awake {'on' if awake else 'off'}")
    log(f"[supervisor] log: {log_path.resolve()}  |  state: {care.HEARTBEAT_FILE}")
    if care.psutil is None:
        log("[supervisor] psutil isn't installed: no memory limits or orphan-browser sweep "
            "(pip install -r requirements.txt)")
    _heartbeat(selected, started)

    threads = []
    for entry in selected.values():
        t = threading.Thread(target=_watch, args=(entry,), name=f"watch-{entry.key}", daemon=True)
        t.start()
        threads.append(t)
    threading.Thread(target=_monitor, args=(selected, started), name="monitor", daemon=True).start()

    try:
        while not _stop.is_set() and any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[supervisor] Ctrl+C received — stopping all managed processes...")
    _stop.set()
    stoppers = [threading.Thread(target=e.stop, daemon=True) for e in selected.values()]
    for s in stoppers:
        s.start()
    for s in stoppers:
        s.join(timeout=CLEAN_STOP_GRACE_SEC + 15)
    for t in threads:
        t.join(timeout=5)
    care.write_heartbeat({**care.read_heartbeat(), "state": "stopped"})
    if awake:
        care.keep_awake(False)
    lock.release()
    log("[supervisor] all stopped.")
    logfile.close()


if __name__ == "__main__":
    main()
