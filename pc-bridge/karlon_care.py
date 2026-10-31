"""karlon_care.py — the housekeeping karlon_supervisor.py needs to run for
weeks without anyone watching: whole-tree process killing, memory readings,
browser (Chromium/Playwright) cleanup, a network probe, keep-awake, and the
single-instance lock + heartbeat that the Windows scheduled task checks.

Everything here is best-effort and never raises into the supervisor: a
cleanup that can't delete a locked file just skips it.

Why each piece exists:

* Job objects (Windows). A child python starts Playwright's driver, which
  starts chrome.exe ×6. TerminateProcess on the python leaves the chromes
  running (Windows has no process groups), still holding the WhatsApp
  profile, so the next start failed with "ProcessSingleton". Each child now
  runs in its own job with KILL_ON_JOB_CLOSE: closing the job kills the whole
  tree, and if the supervisor itself dies the OS closes the handle and the
  trees go with it. No orphans, whatever happens.
* Profile cleanup. After a power cut Chromium leaves Singleton* lock files
  behind and refuses to open the profile; caches (Cache, Code Cache,
  GPUCache …) grow by hundreds of MB. Both are removed before the browser
  starts. The login itself (IndexedDB, Local Storage, Service Worker,
  Cookies) is never touched, so no QR scan is needed.
* Keep-awake. SetThreadExecutionState(ES_SYSTEM_REQUIRED) stops idle sleep
  while the supervisor runs and is released when it exits (shows in
  `powercfg /requests`). Lid-close sleep is a power-plan setting and is set
  by install_karlon.ps1.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

try:
    import psutil                      # optional: memory readings, orphan sweep
except ImportError:                    # pragma: no cover - depends on the machine
    psutil = None

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000

STATE_DIR = Path(os.environ.get("KARLON_STATE_DIR", "") or Path.home() / ".karlon")
LOCK_FILE = STATE_DIR / "supervisor.lock"
HEARTBEAT_FILE = STATE_DIR / "supervisor.json"
STOP_REQUEST = STATE_DIR / "stop.request"
RESTART_REQUEST = STATE_DIR / "restart.request"
HEARTBEAT_STALE_SEC = 180


# ── process trees ─────────────────────────────────────────────────────────

class JobObject:
    """A Windows job object that kills every process in it when closed.
    Elsewhere a no-op (POSIX children get their own session, see popen_kwargs)."""

    def __init__(self) -> None:
        self.handle = None
        if not IS_WINDOWS:
            return
        try:
            import ctypes
            from ctypes import wintypes

            class BASIC(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                            ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD),
                            ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t),
                            ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t),
                            ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class IO(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in
                            ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

            class EXTENDED(ctypes.Structure):
                _fields_ = [("Basic", BASIC), ("Io", IO),
                            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
            k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
            k32.CloseHandle.argtypes = (wintypes.HANDLE,)
            handle = k32.CreateJobObjectW(None, None)
            if not handle:
                return
            info = EXTENDED()
            info.Basic.LimitFlags = 0x2000             # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                k32.CloseHandle(handle)
                return
            self._k32, self.handle = k32, handle
        except Exception:
            self.handle = None

    def add(self, proc: subprocess.Popen) -> bool:
        if not self.handle:
            return False
        try:
            return bool(self._k32.AssignProcessToJobObject(self.handle, int(proc._handle)))
        except Exception:
            return False

    def kill(self) -> None:
        if self.handle:
            try:
                self._k32.TerminateJobObject(self.handle, 1)
            except Exception:
                pass

    def close(self) -> None:
        """Closing the last handle kills whatever is still in the job."""
        if self.handle:
            try:
                self._k32.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None


def popen_kwargs(hidden: bool) -> dict:
    """Process-creation options so a child's whole tree can be killed later:
    its own process group / session, and no console window when unattended."""
    if IS_WINDOWS:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
        if hidden:
            flags |= CREATE_NO_WINDOW
        return {"creationflags": flags}
    return {"start_new_session": True}


def child_python() -> str:
    """The interpreter for children. The scheduled task runs pythonw.exe (no
    console); children need python.exe so their stdout pipe works."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        console = exe.with_name("python.exe")
        if console.exists():
            return str(console)
    return sys.executable


def tree_pids(pid: int) -> list[int]:
    if psutil is None:
        return [pid]
    try:
        p = psutil.Process(pid)
        return [pid] + [c.pid for c in p.children(recursive=True)]
    except psutil.Error:
        return []


def kill_tree(pid: int, timeout: float = 10.0) -> None:
    """Terminate a process and everything it started, then make sure.
    The tree is listed BEFORE the parent dies (afterwards Windows no longer
    links the orphans to it)."""
    if psutil is not None:
        try:
            procs = [psutil.Process(p) for p in tree_pids(pid)]
        except psutil.Error:
            procs = []
        for p in procs:
            try:
                p.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(procs, timeout=timeout)
        for p in alive:
            try:
                p.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(alive, timeout=3)
        return
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.time() + (timeout if sig == signal.SIGTERM else 3)
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.2)


def kill_group(pid: int) -> None:
    """POSIX: kill what's left of a child's process group after it exited
    (its own session, see popen_kwargs). Windows uses the job object."""
    if IS_WINDOWS:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def tree_memory_mb(pid: int) -> float | None:
    """Private memory of a process and all its children (python + driver +
    every chrome), in MB. None when it can't be read."""
    if psutil is None:
        return None
    total = 0
    for p in tree_pids(pid):
        try:
            mi = psutil.Process(p).memory_info()
            total += getattr(mi, "private", 0) or mi.rss
        except psutil.Error:
            pass
    return round(total / 2**20, 1)


def system_memory_percent() -> float | None:
    if psutil is None:
        return None
    return psutil.virtual_memory().percent


def is_supervisor(pid: int) -> bool:
    """Guards against killing an unrelated process that reused a dead pid."""
    if psutil is None:
        return True
    try:
        return "karlon_supervisor" in " ".join(psutil.Process(pid).cmdline())
    except psutil.Error:
        return False


def _orphaned(p) -> bool:
    """Its parent is gone (or the pid now belongs to a newer process)."""
    try:
        parent = p.parent()
        return parent is None or parent.create_time() > p.create_time()
    except psutil.Error:
        return True


def sweep_orphan_browsers(session_dir: str, keep: set[int], profile_only: bool = False,
                          started_before: float | None = None) -> int:
    """Kill browsers no live child owns. Always: any chrome using the WhatsApp
    profile folder outside `keep` (it would block the next start). Unless
    profile_only: Playwright chromes (from the ms-playwright folder) whose
    parent is dead — the browser of a run that was killed. The user's own
    Chrome/Edge (Program Files, no automation) never matches. Returns how
    many were killed. started_before: leave alone anything newer (a browser
    a child is launching right now, not yet in `keep`)."""
    if psutil is None:
        return 0
    session = os.path.normcase(os.path.abspath(session_dir))
    victims = []
    for p in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            if p.pid in keep or "chrom" not in (p.info["name"] or "").lower():
                continue
            if started_before and p.create_time() >= started_before:
                continue
            cmd = os.path.normcase(" ".join(p.info["cmdline"] or []))
            if session in cmd:
                victims.append(p)
            elif not profile_only and "ms-playwright" in (p.info["exe"] or "").lower() and _orphaned(p):
                victims.append(p)
        except psutil.Error:
            continue
    for p in victims:
        try:
            p.kill()
        except psutil.Error:
            pass
    return len(victims)


# ── browser profile and temp cleanup ──────────────────────────────────────

# Rebuilt by Chromium on demand; deleting them costs a little bandwidth on the
# next start and frees hundreds of MB. Paths relative to the profile root and
# to its Default/ folder.
PROFILE_JUNK = ("Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache",
                "GrShaderCache", "GraphiteDawnCache", "ShaderCache", "Crashpad/reports", "Crashpad/attachments",
                "component_crx_cache", "extensions_crx_cache")
PROFILE_LOCKS = ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile")
# Never deleted: this is the login and the chat cache.
PROFILE_KEEP = ("IndexedDB", "Local Storage", "Session Storage", "Cookies", "Network", "Service Worker/Database",
                "Service Worker/ScriptCache", "Preferences", "Local State")
TEMP_PATTERNS = ("playwright_chromiumdev_profile-*", "playwright-artifacts-*", "playwright_firefoxdev_profile-*",
                 "scoped_dir*")


def _size(p: Path) -> int:
    if p.is_file() or p.is_symlink():
        try:
            return p.lstat().st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _remove(p: Path) -> int:
    """Delete a file or folder; whatever is locked stays. Returns bytes freed."""
    if not (p.exists() or p.is_symlink()):
        return 0
    before = _size(p)
    try:
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink()
    except OSError:
        pass
    return max(0, before - (_size(p) if p.exists() else 0))


def clean_profile(session_dir: str, caches: bool = True) -> int:
    """Stale locks (always) and caches (if caches=True) of a Chromium profile
    that is NOT running. Returns bytes freed."""
    root = Path(session_dir)
    if not root.is_dir():
        return 0
    freed = 0
    for name in PROFILE_LOCKS:
        freed += _remove(root / name)
    if caches:
        for base in (root, root / "Default"):
            for name in PROFILE_JUNK:
                freed += _remove(base / name)
        for f in list(root.glob("BrowserMetrics*")) + list(root.glob("*.pma")) + list(root.glob("Crash*Reports")):
            freed += _remove(f)
    return freed


def _newest_mtime(p: Path) -> float:
    newest = p.lstat().st_mtime
    for root, dirs, files in os.walk(p):
        for name in dirs + files:
            try:
                newest = max(newest, os.lstat(os.path.join(root, name)).st_mtime)
            except OSError:
                pass
    return newest


def browser_paths_in_use() -> str:
    """Command lines of every running Chromium, lower-cased, for in-use checks."""
    if psutil is None:
        return ""
    out = []
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if "chrom" in (p.info["name"] or "").lower():
                out.append(" ".join(p.info["cmdline"] or []))
        except psutil.Error:
            continue
    return os.path.normcase("\n".join(out))


def clean_temp(temp_dir: str | None = None, older_than_sec: int = 3600, in_use: str | None = None) -> int:
    """Leftover Playwright/Chromium temp profiles (one per browser launch that
    didn't close cleanly). Only folders no running browser names on its
    command line and with nothing inside touched for older_than_sec, so a
    live scraper's profile is never half-deleted."""
    base = Path(temp_dir or os.environ.get("TEMP") or os.environ.get("TMPDIR") or "/tmp")
    cutoff = time.time() - older_than_sec
    in_use = browser_paths_in_use() if in_use is None else in_use
    freed = 0
    for pattern in TEMP_PATTERNS:
        for p in base.glob(pattern):
            try:
                if os.path.normcase(p.name) in in_use or _newest_mtime(p) >= cutoff:
                    continue
                freed += _remove(p)
            except OSError:
                pass
    return freed


def prune_logs(log_dir: str, days: int = 14) -> int:
    """Old supervisor logs and wa_bridge debug screenshots. Returns files removed."""
    cutoff = time.time() - days * 86400
    removed = 0
    for pattern in ("karlon_*.log*", "wa_*.png", "wa_*.txt"):
        for p in Path(log_dir).glob(pattern):
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                pass
    return removed


# ── network ───────────────────────────────────────────────────────────────

def net_ok(url: str, timeout: float = 4.0) -> bool:
    """Can we open a TCP connection to the server (DNS + connect)? Cheap
    enough to run every few seconds; no HTTP request is made."""
    u = urlparse(url)
    host = u.hostname or "localhost"
    port = u.port or (443 if u.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ── power ─────────────────────────────────────────────────────────────────

def keep_awake(on: bool = True) -> bool:
    """Ask Windows not to idle-sleep while this thread lives (or release)."""
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception:
        return False


def session_id(pid: int | None = None) -> int:
    """Windows session of a process: 0 = no desktop (started at boot, before
    logon), 1+ = a logged-on user's desktop. Always 0 elsewhere."""
    if not IS_WINDOWS:
        return 0
    try:
        import ctypes
        out = ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.ProcessIdToSessionId(pid or os.getpid(), ctypes.byref(out))
        return int(out.value) if ok else -1
    except Exception:
        return -1


# ── single instance + heartbeat ───────────────────────────────────────────

class InstanceLock:
    """An OS file lock: held while this process lives, released by the OS
    when it dies (even on a hard kill), so a stale lock never blocks a start."""

    def __init__(self, path: Path = LOCK_FILE) -> None:
        self.path = path
        self.fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if IS_WINDOWS:
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self.fh = fh
        return True

    def release(self) -> None:
        if self.fh:
            try:
                self.fh.close()
            except OSError:
                pass
            self.fh = None


def read_heartbeat(path: Path = HEARTBEAT_FILE) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def heartbeat_age(beat: dict, now: float | None = None) -> float:
    try:
        return (now or time.time()) - float(beat["updated"])
    except (KeyError, TypeError, ValueError):
        return float("inf")


def write_heartbeat(data: dict, path: Path = HEARTBEAT_FILE) -> None:
    """Atomic write (temp file + replace), so a reader never sees half a file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({**data, "updated": time.time()}, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def take_request(path: Path) -> bool:
    """True once if a request file (stop/restart) is present; removes it."""
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass
        return True
    return False


class RotatingLog:
    """One log file that rolls over at max_mb (karlon_service.log → .1 → .2 …),
    so an always-on service never fills the disk."""

    def __init__(self, path: Path, max_mb: int = 10, keep: int = 5) -> None:
        self.path, self.max_bytes, self.keep = Path(path), max_mb * 2**20, keep
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a", encoding="utf-8", errors="replace")

    def write(self, text: str) -> None:
        try:
            self.fh.write(text)
            self.fh.flush()
            if self.fh.tell() > self.max_bytes:
                self._roll()
        except (OSError, ValueError):
            pass

    def _roll(self) -> None:
        self.fh.close()
        for i in range(self.keep - 1, 0, -1):
            src = self.path.with_name(f"{self.path.name}.{i}")
            if src.exists():
                os.replace(src, self.path.with_name(f"{self.path.name}.{i + 1}"))
        os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
        self.fh = open(self.path, "a", encoding="utf-8", errors="replace")

    def flush(self) -> None:
        try:
            self.fh.flush()
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        try:
            self.fh.close()
        except OSError:
            pass
