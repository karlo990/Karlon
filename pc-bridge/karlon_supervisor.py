"""karlon_supervisor.py — one console, three managed processes.

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

RUN:
    python karlon_supervisor.py                  # all three
    python karlon_supervisor.py --only wa,invoice # a subset, for debugging
    KARLON_URL=http://127.0.0.1:8000 python karlon_supervisor.py

Ctrl+C once, here, stops all three cleanly.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from local_config import KARLON_URL, LOG_DIR, print_active_config

# One-for-one restart with exponential backoff: 5s, 10s, 20s ... capped at
# MAX_BACKOFF_SEC. A run that stayed up at least STABLE_RUN_SEC counts as
# healthy and resets the ladder, so a process that crashes once a day always
# restarts in 5s, while one that dies on launch backs off to a slow retry
# instead of hammering the server (or Airbnb, or WhatsApp Web) every 30s.
RESTART_BACKOFF_SEC = 5
MAX_BACKOFF_SEC = 300
STABLE_RUN_SEC = 60


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
    proc: subprocess.Popen | None = None
    last_restart: float = 0.0
    restart_count: int = 0        # lifetime total, for the log line
    consecutive_fast_failures: int = 0

    def start(self) -> None:
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
            **self.env,
        }
        self.proc = subprocess.Popen(
            [sys.executable, "-u", self.script, *self.args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=proc_env,
        )
        self.last_restart = time.time()


# The three processes this supervisor knows about. Add/remove entries here
# (not by hand-editing the watch loop) if the set of managed scripts ever
# changes.
ALL_PROCESSES: dict[str, ManagedProcess] = {
    "wa":      ManagedProcess(key="wa",      script="wa_bridge.py"),
    "invoice": ManagedProcess(key="invoice", script="invoice_worker.py"),
    "scraper": ManagedProcess(
        key="scraper", script="airbnb_parallel_system.py",
        env={"KARLON_SCRAPER_NONINTERACTIVE": "1"},
    ),
}

_stop = threading.Event()


def compute_backoff(consecutive_fast_failures: int) -> int:
    """Seconds to wait before restart number `consecutive_fast_failures + 1`."""
    return min(MAX_BACKOFF_SEC, RESTART_BACKOFF_SEC * (2 ** consecutive_fast_failures))


def _reader_thread(proc_entry: ManagedProcess, log_fh) -> None:
    """Forwards one child's stdout to this console (and the combined log
    file), prefixed with its tag, until the child exits or we're stopping.
    Wrapped so a single unprintable line can never kill the reader (and with
    it the restart logic) again."""
    assert proc_entry.proc is not None
    try:
        for line in proc_entry.proc.stdout:
            tagged = f"[{proc_entry.key}] {line.rstrip()}"
            try:
                print(tagged, flush=True)
            except UnicodeEncodeError:
                print(tagged.encode("ascii", "replace").decode("ascii"), flush=True)
            try:
                log_fh.write(tagged + "\n")
                log_fh.flush()
            except Exception:
                pass
    except Exception as e:
        print(f"[supervisor] reader for {proc_entry.key} stopped: {e!r}", flush=True)


def _watch(proc_entry: ManagedProcess, log_fh) -> None:
    """Owns one process's whole lifecycle: start, forward output, and on
    exit — unless we're shutting down — restart it after a backoff that
    grows if it's crash-looping."""
    while not _stop.is_set():
        proc_entry.start()
        print(f"[supervisor] started {proc_entry.key} ({proc_entry.script}), pid={proc_entry.proc.pid}")
        reader = threading.Thread(target=_reader_thread, args=(proc_entry, log_fh), daemon=True)
        reader.start()

        exit_code = proc_entry.proc.wait()
        reader.join(timeout=2)

        if _stop.is_set():
            print(f"[supervisor] {proc_entry.key} exited (code {exit_code}) during shutdown — not restarting")
            return

        uptime = time.time() - proc_entry.last_restart
        if uptime >= STABLE_RUN_SEC:
            proc_entry.consecutive_fast_failures = 0
        backoff = compute_backoff(proc_entry.consecutive_fast_failures)
        proc_entry.consecutive_fast_failures += 1
        proc_entry.restart_count += 1

        print(
            f"[supervisor] ! {proc_entry.key} exited unexpectedly (code {exit_code}). "
            f"Restarting in {backoff}s (restart #{proc_entry.restart_count})"
        )
        # Wait in small increments so a Ctrl+C during the backoff still stops promptly.
        waited = 0
        while waited < backoff and not _stop.is_set():
            time.sleep(1)
            waited += 1


def _force_utf8_console() -> None:
    """Make this console itself UTF-8 so the forwarded lines print cleanly
    (PowerShell defaults to cp1252 for Python's stdout)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main() -> None:
    _force_utf8_console()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--only",
        help="comma-separated subset to run, e.g. 'wa,invoice' (default: all three)",
    )
    args = ap.parse_args()

    print_active_config()

    selected_keys = args.only.split(",") if args.only else list(ALL_PROCESSES.keys())
    unknown = [k for k in selected_keys if k not in ALL_PROCESSES]
    if unknown:
        print(f"[supervisor] unknown key(s) {unknown} — choose from {list(ALL_PROCESSES.keys())}")
        return
    selected = {k: ALL_PROCESSES[k] for k in selected_keys}

    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
    log_path = Path(LOG_DIR) / f"karlon_{datetime.now():%Y%m%d_%H%M%S}.log"
    print(f"[supervisor] managing: {', '.join(selected)}  |  KARLON_URL={KARLON_URL}")
    print(f"[supervisor] combined log: {log_path}")

    with open(log_path, "a", encoding="utf-8") as log_fh:
        threads = []
        for entry in selected.values():
            t = threading.Thread(target=_watch, args=(entry, log_fh), daemon=True)
            t.start()
            threads.append(t)

        try:
            while any(t.is_alive() for t in threads):
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[supervisor] Ctrl+C received — stopping all managed processes...")
            _stop.set()
            for entry in selected.values():
                if entry.proc and entry.proc.poll() is None:
                    entry.proc.terminate()
            for entry in selected.values():
                if entry.proc:
                    try:
                        entry.proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        entry.proc.kill()
            for t in threads:
                t.join(timeout=5)
            print("[supervisor] all stopped.")


if __name__ == "__main__":
    main()
