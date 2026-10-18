"""karlon_login.py — the one-time logins, in visible windows, so everything
else can run headless (no window, in the background).

    python karlon_login.py                 # WhatsApp, then Airbnb
    python karlon_login.py --only wa       # just WhatsApp (scan the QR again)
    python karlon_login.py --only airbnb   # just Airbnb

1. Pauses the running supervisor (it holds the WhatsApp profile) and keeps
   the scheduled task from restarting it until you're done.
2. WhatsApp: a window opens. Scan the QR (WhatsApp on your phone > Linked
   devices > Link a device). When your chats show, press Enter here: the
   login is saved in ~/.karlon/wa_session.
3. Airbnb: a window opens. Log in, then press Enter here: the login is saved
   in airbnb_data/auth_state.json.
4. Starts Karlon again. From now on WhatsApp and the scraper run headless
   with the saved logins (KARLON_WA_HEADLESS / KARLON_AIRBNB_HEADLESS = auto).
   If a login expires later, that app opens a window again by itself, or run
   this script again.

Invoices need no login: the invoice worker always prints its PDFs with a
headless browser.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.chdir(HERE)                          # airbnb_data/ and logs/ are relative to this folder
sys.path.insert(0, str(HERE))

import karlon_care as care              # noqa: E402
from local_config import AIRBNB_AUTH_STATE_FILE, CHROMIUM_ARGS, wa_login_marker  # noqa: E402

HOLD_FILE = care.STATE_DIR / "login.hold"


def _enter_event() -> threading.Event:
    """Set when Enter is pressed (re-armed by calling again)."""
    ev = threading.Event()

    def wait():
        try:
            input()
            ev.set()
        except EOFError:
            pass
    threading.Thread(target=wait, daemon=True).start()
    return ev


def pause_supervisor() -> bool:
    """Ask a running supervisor to stop and hold it off. True if one was running."""
    care.STATE_DIR.mkdir(parents=True, exist_ok=True)
    HOLD_FILE.write_text(str(os.getpid()), encoding="utf-8")
    beat = care.read_heartbeat()
    if care.heartbeat_age(beat) > care.HEARTBEAT_STALE_SEC or beat.get("state") == "stopped":
        return False
    print("Pausing Karlon (it closes WhatsApp cleanly; up to a minute)...")
    care.STOP_REQUEST.touch()
    deadline = time.time() + 120
    while time.time() < deadline:
        beat = care.read_heartbeat()
        if beat.get("state") == "stopped" or care.heartbeat_age(beat) > 60:
            print("  paused.")
            return True
        time.sleep(2)
    print("  it did not stop in time; close any WhatsApp browser window, then continue.")
    return True


def resume_supervisor() -> None:
    try:
        HOLD_FILE.unlink()
    except OSError:
        pass
    if care.IS_WINDOWS:
        r = subprocess.run(["schtasks", "/Run", "/TN", r"\Karlon\Karlon"], capture_output=True, text=True)
        if r.returncode == 0:
            print("Karlon started again (scheduled task). Check it with: .\\karlon_status.ps1")
            return
    # Not installed as a task (or no right to run it from here): start the
    # supervisor in the background ourselves, no window, detached from this one.
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    cmd = [str(pyw if pyw.exists() else exe), str(HERE / "karlon_supervisor.py"), "--service", "--takeover"]
    flags = (0x00000008 | 0x00000200) if care.IS_WINDOWS else 0          # DETACHED_PROCESS | NEW_PROCESS_GROUP
    try:
        subprocess.Popen(cmd, cwd=HERE, creationflags=flags, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=not care.IS_WINDOWS)
        print("Karlon started again in the background. Check it with: .\\karlon_status.ps1")
    except OSError as e:
        print(f"Could not start Karlon ({e}). Start it with:  python karlon_supervisor.py")


def login_whatsapp() -> bool:
    from playwright.sync_api import sync_playwright
    import wa_bridge as wa

    print("\n=== WhatsApp ===")
    print(f"Login folder: {wa.SESSION_DIR}")
    care.clean_profile(wa.SESSION_DIR, caches=False)          # stale lock files from a hard stop
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(user_data_dir=wa.SESSION_DIR, headless=False,
                                                    viewport={"width": 1400, "height": 900}, args=CHROMIUM_ARGS)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=90_000)
        try:
            page.bring_to_front()
        except Exception:
            pass
        print("In the window: if a QR code shows, scan it with WhatsApp on your phone")
        print("(Settings > Linked devices > Link a device).")
        print("When your chats are showing, press Enter here to save the login.")
        enter, said_ready, ok = _enter_event(), False, False
        while True:
            kind, _ = wa.whatsapp_state(page)
            if kind == "chats" and not said_ready:
                print("  Logged in - your chats are showing. Press Enter to save and close.")
                said_ready = True
            if enter.is_set():
                if kind == "chats":
                    ok = True
                    break
                print("  Your chats aren't showing yet - finish the login in the window, then press Enter again.")
                enter = _enter_event()
            time.sleep(1)
        print("  Saving (letting WhatsApp finish syncing for a few seconds)...")
        time.sleep(8)
        ctx.close()                                            # a clean close writes the profile to disk
    if ok:
        marker = wa_login_marker(wa.SESSION_DIR)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
        print("  WhatsApp login saved. It now runs headless in the background.")
    return ok


async def _login_airbnb() -> bool:
    from playwright.async_api import async_playwright
    import airbnb_parallel_system as ab

    print("\n=== Airbnb ===")
    async with async_playwright() as p:
        browser = await ab.launch_browser(p, headless=False)
        try:
            ctx = await ab.try_restore_session(browser)
            if ctx is not None:
                await ab.save_session(ctx)
                print("  Already logged in - login refreshed.")
                return True
            print("In the window: log in to Airbnb. When you're logged in, press Enter here.")
            ctx = await ab.login_with_timeout(browser)
            if ctx is None:
                return False
            await ab.save_session(ctx)
            # Same check the scraper makes when it starts: a fresh browser
            # context from the saved file, given time to load.
            check = await ab.try_restore_session(browser)
            if check is None:
                print("  Saved, but a fresh check didn't see you logged in yet. If the scraper later asks")
                print("  to log in, run:  python karlon_login.py --only airbnb")
            else:
                await check.close()
            print(f"  Airbnb login saved ({AIRBNB_AUTH_STATE_FILE}). The scraper now runs headless.")
            return True
        finally:
            await browser.close()


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", choices=["wa", "airbnb"], help="just one of the two logins")
    args = ap.parse_args()

    pause_supervisor()
    results = {}
    try:
        if args.only in (None, "wa"):
            results["WhatsApp"] = login_whatsapp()
        if args.only in (None, "airbnb"):
            results["Airbnb"] = asyncio.run(_login_airbnb())
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        print()
        for name, ok in results.items():
            print(f"  {name}: {'saved - runs headless' if ok else 'NOT saved - run again'}")
        resume_supervisor()


if __name__ == "__main__":
    main()
