"""
wa_bridge.py — mirrors WhatsApp Web chats into Karlon AND delivers
messages sent from the Karlon app/web back to WhatsApp via DOM automation.

Architecture
============
┌──────────────────────────────────────────────────────────────┐
│  Main thread  (Playwright / sync)                            │
│  ┌─────────────────────────┐  ┌───────────────────────────┐ │
│  │  sync_once()            │  │  process_outbox()         │ │
│  │  WA → Karlon            │  │  Karlon → WA via DOM      │ │
│  │  (import messages)      │  │  (drain _outbox queue)    │ │
│  └─────────────────────────┘  └───────────────────────────┘ │
└───────────────────────────┬──────────────────────────────────┘
                            │  _outbox (queue.Queue)
┌───────────────────────────▼──────────────────────────────────┐
│  Outbox-poller thread  (requests / non-Playwright)           │
│  Polls GET /api/outbox every 5 s.                            │
│  Deduplicates by message-id.                                 │
│  Puts pending items into _outbox for the main thread.        │
└──────────────────────────────────────────────────────────────┘

The two threads share only _outbox (thread-safe queue.Queue) and
_queued_ids (a set guarded by _ids_lock).  All Playwright calls
happen exclusively on the main thread.

SETUP:
    pip install playwright requests
    playwright install chromium

ENVIRONMENT:
    KARLON_URL=http://localhost:8000   (default: HF Space URL)

RUN:
    python wa_bridge.py --once --all           # one-time full clone
    python wa_bridge.py --once --all --deep-history  # one-time full clone, scroll every chat back much further (use once, not on the routine loop)
    python wa_bridge.py                # continuous sync
    python wa_bridge.py --pics         # include profile pictures
    python wa_bridge.py --headless     # hide browser
    python wa_bridge.py --discover     # debug selector discovery
"""

import sys

# Windows PowerShell's console defaults to the cp1252 codepage, which can't
# encode emoji (e.g. the 📷 printed in sync_once()'s image-tag label). Without
# this, the script crashes with UnicodeEncodeError on the first synced chat
# that contains an image, causing the supervisor to restart wa_bridge.py in
# a loop instead of ever completing a sync pass. Force UTF-8 on stdout/stderr
# before any print() runs.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

import argparse
import base64
import hashlib
import queue
import re
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import os
import requests
from playwright.sync_api import Page, sync_playwright

# ─────────────────────────────────── config ───────────────────────────────────

from karlon_client import DOWNLOAD_TIMEOUT, HTTP_TIMEOUT, UPLOAD_TIMEOUT, get_session
from local_config import (
    KARLON_URL, WA_POLL_INTERVAL, WA_OUTBOX_POLL_SEC, WA_FULL_SWEEP_EVERY,
    print_active_config,
)

# PowerShell's default code page can't encode ✓ ✗ or emoji — reconfigure
# stdout/stderr so prints never raise UnicodeEncodeError (karlon_supervisor
# also sets PYTHONIOENCODING/PYTHONUTF8 for us, this covers running by hand).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SESSION_DIR         = "./wa_session"
POLL_INTERVAL       = WA_POLL_INTERVAL     # seconds between WA→Karlon import cycles
OUTBOX_POLL_SEC     = WA_OUTBOX_POLL_SEC   # seconds between outbox REST polls
PROFILE_PICS_DIR    = Path("./static/profile_pics")
INCOMING_IMAGES_DIR = Path("./wa_incoming_images")

FLAG_KEYWORDS      = ["new order", "pending payment", "invoice", "deposit", "paid"]
FLAG_NAME_PREFIXES = ["+27", "+263"]

_PHONE_RE    = re.compile(r"^\+\d[\d\s\-().]{6,}$")
_WA_HEADER_RE = re.compile(r"^\s*(\d+\s+unread\s+messages?|archived)\s*$", re.IGNORECASE)

# Generic boilerplate WA puts as the `title` attribute on the header's
# clickable wrapper (contact-info / group-info entry point) — never a real
# contact name, so get_open_chat_title() must skip past these rather than
# returning the first title-bearing span it finds.
_HEADER_TITLE_DENYLIST = {
    "click here for contact info",
    "click here for group info",
    "click here for more info",
}

# ─────────────────────────────── outbox queue ─────────────────────────────────

_outbox: "queue.Queue[dict]" = queue.Queue()
_queued_ids: set[str] = set()      # dedup: IDs already in the queue
_ids_lock = threading.Lock()       # guards _queued_ids
MAX_RETRIES = 3                    # before giving up and marking error

# ── Urgent reply queue — polled at 2 s, drained BEFORE normal outbox ──────────
# Messages land here when sent via POST /api/reply/{chat_id} (wa_status='urgent').
# Processed with priority in process_outbox() so customer replies are never
# held behind bulk/broadcast traffic in the normal _outbox queue.
URGENT_OUTBOX_POLL_SEC = 2

_urgent_outbox: "queue.Queue[dict]" = queue.Queue()
_urgent_queued_ids: set[str] = set()
_urgent_ids_lock = threading.Lock()

# ─────────────────────────────── selectors ────────────────────────────────────

_ROW_SELECTOR_CANDIDATES = [
    '[data-testid="cell-frame-container"]',
    '[data-testid="list-item-container"]',
    '[data-testid="conversation-list-item"]',
    '#pane-side [role="listitem"]',
    '[aria-label][tabindex="0"][data-testid]',
]

_TITLE_SELECTOR_CANDIDATES = [
    '[data-testid="cell-frame-title"]',
    '[data-testid="contact-title"]',
    'span[title][dir="auto"]',
    'span[title]',
    '[dir="auto"][title]',
]

_PREVIEW_SELECTOR_CANDIDATES = [
    '[data-testid="cell-frame-secondary"]',
    '[data-testid="last-message"]',
    'span[data-testid="last-msg-status"] + span',
    'div[class*="last-message"]',
]

# WhatsApp Web sidebar search — icon/button that opens the search input,
# the input itself, and the "x" that clears it back to the normal chat
# list. WA has used both a plain <input> and a contenteditable div for this
# across versions — try both.
_SEARCH_BUTTON_SELECTORS = [
    '[data-testid="chat-list-search"]',
    'button[aria-label="Search"]',
    'div[title="Search"]',
    'span[data-icon="search"]',
]
_SEARCH_INPUT_SELECTORS = [
    '[data-testid="chat-list-search"] input',
    'div[contenteditable="true"][data-tab="3"]',
    'div[aria-label="Search input textbox"]',
    'input[aria-label="Search input textbox"]',
    'div[aria-label="Search or start new chat"]',
    'p[title="Search"]',
]
_SEARCH_CLEAR_SELECTORS = [
    '[data-testid="x-alt"]',
    'button[aria-label="Cancel search"]',
    'span[data-icon="x-alt"]',
]

_CHATLIST_CONTAINER = '#pane-side, [data-testid="chat-list"], [aria-label*="Chat list"]'

# WhatsApp Web compose box — tried in order
_COMPOSE_SELECTORS = [
    'div[contenteditable="true"][data-tab="10"]',
    'div[aria-label="Type a message"][contenteditable="true"]',
    '#main footer div[contenteditable="true"]',
    'footer div[contenteditable="true"]',
    '[data-testid="conversation-compose-box-input"]',
]

# WhatsApp Web send button — tried in order
_SEND_SELECTORS = [
    'button[data-testid="send"]',
    'span[data-testid="send"]',
    '[data-testid="send"]',
    'button[aria-label="Send"]',
    '#main [aria-label="Send"]',
]

# Attach ("+" / paperclip) button that opens the photo/document/etc. menu.
# WA has renamed this from a paperclip to a "+" a few times — keep both.
_ATTACH_BUTTON_SELECTORS = [
    'button[data-testid="clip"]',
    'span[data-testid="clip"]',
    '[data-testid="clip"]',
    'button[title="Attach"]',
    'div[title="Attach"]',
    '[aria-label="Attach"]',
]

# The menu item labelled "Document" in the attach popup. Its click target
# is usually the visible <li>/<div>, NOT the hidden <input type=file> that
# sits behind it — Playwright's set_input_files works on the hidden input
# directly without needing this click at all, but WA only *renders* the
# document-specific input into the DOM after this menu opens, so we still
# need to open it first even though we never click this specific item.
_ATTACH_MENU_SELECTORS = [
    '[data-testid="attach-menu"]',
    'div[role="application"] ul',
    'ul[role="listbox"]',
    'div[aria-label="Attach"] + div',
]

# Hidden file inputs WA renders once the attach menu is open. The document
# input generally has the broadest/most permissive `accept` (it takes any
# file type), while the photo/video input restricts to image/video mimes —
# that's the signal used to pick the right one when both are present.
_DOCUMENT_INPUT_SELECTORS = [
    'input[type="file"][accept*="pdf"]',
    'input[accept="*"]',
    'input[type="file"]:not([accept*="image"])',
    'input[type="file"]',
]
_IMAGE_INPUT_SELECTORS = [
    'input[type="file"][accept*="image"]',
    'input[type="file"]',
]

# Send button inside the media/document preview modal (distinct DOM from
# the plain-text compose send button above, though WA sometimes reuses it).
_MEDIA_SEND_SELECTORS = [
    'div[role="button"][aria-label="Send"]',
    'span[data-icon="send"]',
    'button[aria-label="Send"]',
    '[data-testid="send"]',
] + _SEND_SELECTORS

# The "send as sticker" toggle that appears in the image-preview toolbar.
# It is a real, focusable button — if keyboard focus is ever left sitting
# on it (e.g. no caption was typed, so nothing else claimed focus), a bare
# Enter press activates *this* instead of sending the photo. We check for
# it explicitly rather than ever risking a blind Enter near it.
_STICKER_TOGGLE_SELECTORS = [
    '[data-testid="send-as-sticker-toggle"]',
    'div[aria-label="Send as sticker"]',
    'button[aria-label="Send as sticker"]',
    '[data-icon="sticker"]',
]

# ─────────────────────────────── helpers ──────────────────────────────────────

def is_unsaved_number(name: str) -> bool:
    return bool(_PHONE_RE.match(name.strip()))

def is_wa_section_header(name: str) -> bool:
    return bool(_WA_HEADER_RE.match(name))

def avatar_tag(name: str) -> str:
    if is_unsaved_number(name):
        digits = re.sub(r"[^\d]", "", name)
        return digits[-2:] if len(digits) >= 2 else "??"
    letters = re.sub(r"[^A-Za-z0-9]", "", name)[:2].upper()
    return letters or "WA"

def _safe_inner_text(el, timeout_ms: int = 2_000) -> str:
    try:
        return el.inner_text().strip()
    except Exception:
        return ""

def _safe_get_attr(el, attr: str) -> str:
    try:
        return (el.get_attribute(attr) or "").strip()
    except Exception:
        return ""

# ─────────────────────────────── Karlon REST ──────────────────────────────────

# WhatsApp's "click-to-WhatsApp ad" context card — the auto-attached
# Instagram/Facebook ad blurb WhatsApp shows above a lead's first message
# when they messaged in from an ad. WhatsApp itself renders it as a clean
# multi-line card (business name, offer line, location, "Hosted by", link),
# but scraping it via inner_text() collapses it into one dense paragraph
# with no line breaks, so it lands in Karlon looking like a wall of text.
_AD_CARD_MARKER_RE = re.compile(r"[📍🏠🌴💬📅✨🛏️]")
_AD_CARD_BREAK_RE  = re.compile(r"\s*(?<![📍🏠🌴💬📅✨🛏️])(?=[📍🏠🌴💬📅✨🛏️])")
_AD_CARD_LINK_RE   = re.compile(r"\s*((?:www\.)?(?:instagram|facebook)\.com\S*)\s*$")


def reformat_ad_context_card(text: str) -> str:
    """Detects a flattened ad-context card and breaks it back into lines.

    Deliberately conservative — only touches text that (a) has no existing
    newlines, (b) carries at least two of the card's marker emoji, and
    (c) ends in a bare instagram.com/facebook.com link — the exact shape of
    this specific WhatsApp widget. Ordinary messages (which rarely hit all
    three at once) pass through unchanged.
    """
    if "\n" in text:
        return text
    if len(_AD_CARD_MARKER_RE.findall(text)) < 2:
        return text
    if not _AD_CARD_LINK_RE.search(text):
        return text

    spaced = _AD_CARD_BREAK_RE.sub("\n", text)
    spaced = _AD_CARD_LINK_RE.sub(r"\n\1", spaced)
    lines = [ln.strip() for ln in spaced.split("\n") if ln.strip()]
    return "\n".join(lines)


def chat_slug(name: str) -> str:
    h = hashlib.md5(name.strip().encode()).hexdigest()[:12]
    return f"wa-{h}"

def ensure_chat(name: str, pic_url: Optional[str] = None) -> str:
    """
    Create/update a chat in Karlon.
    - `name`     : raw WhatsApp name (stored as wa_name for DOM lookup later)
    - display    : pretty name shown in the Karlon UI
    """
    cid = chat_slug(name)
    # No more emoji-prefixed display name — unsaved numbers show as the
    # plain number now. Which ones are "unsaved" is surfaced to clients as
    # its own `is_unsaved` boolean (see routers/chats.py) so the UI can mark
    # them with a proper visual indicator instead of a character glued onto
    # the name string.
    display = name
    payload: dict = {
        "id":           cid,
        "name":         display,
        "avatar_emoji": avatar_tag(name),
        "wa_name":      name,         # raw WA name — used by outbox sender
    }
    if pic_url:
        payload["profile_pic_url"] = pic_url
    # HF Spaces cold-start / go briefly unresponsive under load, which was
    # showing up as read timeouts here. A single failed call used to bubble
    # straight up to sync_once() and abort the whole sync pass (every chat
    # after the one that hit it never got processed that cycle). Retry a
    # couple times with backoff before giving up — only a real, sustained
    # outage should still propagate.
    last_exc: Optional[requests.RequestException] = None
    for attempt, backoff in enumerate((0, 2, 5)):
        if backoff:
            time.sleep(backoff)
        try:
            get_session().post(f"{KARLON_URL}/api/chats", json=payload, timeout=HTTP_TIMEOUT).raise_for_status()
            return cid
        except requests.RequestException as e:
            last_exc = e
    raise last_exc

def import_messages(chat_id: str, messages: list[dict]) -> dict:
    if not messages:
        return {"imported": 0, "skipped": 0}
    r = get_session().post(
        f"{KARLON_URL}/api/chats/{chat_id}/import",
        json={"messages": messages},
        timeout=UPLOAD_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()

def _ack_wa_message(chat_id: str, msg_id: str, status: str = "sent") -> None:
    """Tell Karlon the WA-delivery result for one outbound message.

    The message is already on WhatsApp by the time we ack "sent". A single
    lost ack used to leave it 'pending' server-side, and the next bridge
    restart (fresh dedup set) would send it to the customer a second time.
    The PATCH just sets a status, so replaying it is safe — retry it here
    (the transport layer won't, for non-GET methods)."""
    for attempt, backoff in enumerate((0, 1, 3)):
        if backoff:
            time.sleep(backoff)
        try:
            r = get_session().patch(
                f"{KARLON_URL}/api/chats/{chat_id}/messages/{msg_id}/wa-ack",
                params={"status": status},
                timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
            return
        except Exception as e:
            print(f"  [outbox] ack failed ({status}, attempt {attempt + 1}/3): {e}")

# ─────────────────────────────── DOM send ─────────────────────────────────────

def dom_send_message(page: Page, text: str) -> bool:
    """
    Type `text` into the currently-open WA chat compose box and press Send.

    Strategy:
    1. Find the contenteditable compose box using multiple selector candidates.
    2. Click it to focus, clear any existing content, type the message.
    3. Click the Send button (or fall back to Enter).
    4. Wait briefly for WA to register the send.

    Returns True on success, False if any step fails.
    """
    if not text or not text.strip():
        return False

    # ── 1. locate compose box ────────────────────────────────────────────────
    compose_box = None
    for sel in _COMPOSE_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                compose_box = el
                break
        except Exception:
            continue

    if not compose_box:
        print("  [dom_send] compose box not found — is a chat open?")
        return False

    try:
        # ── 2. focus + clear + type ──────────────────────────────────────────
        compose_box.click()
        page.wait_for_timeout(200)

        # Select-all + Delete clears any pre-existing text in the contenteditable
        page.keyboard.press("Control+a")
        page.keyboard.press("Delete")
        page.wait_for_timeout(100)

        # Use keyboard.type so special characters survive (keyboard.fill works
        # for <input> but often doesn't trigger WA's onChange for contenteditable)
        page.keyboard.type(text, delay=15)
        page.wait_for_timeout(350)

        # ── 3. send ──────────────────────────────────────────────────────────
        send_btn = None
        for sel in _SEND_SELECTORS:
            try:
                btn = page.query_selector(sel)
                if btn:
                    send_btn = btn
                    break
            except Exception:
                continue

        if send_btn:
            send_btn.click()
        else:
            # Fallback: Enter key (shift+enter would insert a newline)
            page.keyboard.press("Enter")

        page.wait_for_timeout(700)   # let WA register the outgoing message
        return True

    except Exception as e:
        print(f"  [dom_send] error: {e}")
        return False

# ─────────────────────────────── DOM send: file/document ──────────────────────

def dom_send_file(page: Page, file_path: str, caption: str = "", kind: str = "document") -> bool:
    """
    Attach and send a local file (image or document, e.g. a generated
    invoice PDF) to the currently-open WA chat.

    Strategy:
    1. Click the attach ("+") button to open the menu — this is what
       makes WA render the hidden file <input> elements into the DOM.
    2. Locate the right hidden input (document vs image accept-type
       candidates) and call set_input_files directly on it — Playwright
       can do this even though the input is visually hidden, so we don't
       need to click the "Document" menu item itself.
    3. Wait for the preview modal, optionally type a caption.
    4. Click Send.

    Returns True on success, False if any step fails. Like dom_send_message,
    every selector here is a best guess at WA Web's current DOM and is the
    first thing to check if this stops working after a WhatsApp update.
    """
    if not file_path or not Path(file_path).exists():
        print(f"  [dom_send_file] file not found: {file_path}")
        return False

    try:
        attach_btn = None
        for sel in _ATTACH_BUTTON_SELECTORS:
            try:
                el = page.query_selector(sel)
                if el:
                    attach_btn = el
                    break
            except Exception:
                continue
        if not attach_btn:
            print("  [dom_send_file] attach button not found")
            return False

        attach_btn.click()
        page.wait_for_timeout(400)

        input_candidates = _DOCUMENT_INPUT_SELECTORS if kind == "document" else _IMAGE_INPUT_SELECTORS
        file_input = None
        for sel in input_candidates:
            try:
                el = page.query_selector(sel)
                if el:
                    file_input = el
                    break
            except Exception:
                continue

        if not file_input:
            print(f"  [dom_send_file] no matching file input found for kind='{kind}'")
            # Close the menu (Escape) so we don't leave the UI in a stuck state.
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return False

        file_input.set_input_files(file_path)
        page.wait_for_timeout(1_200)  # preview modal render + upload thumbnail

        # Never leave focus wherever the attach flow happens to drop it.
        # Click the previewed media itself first — a neutral element that
        # can't accidentally toggle anything — so a caption click or a
        # fallback keypress never lands on a toolbar icon by accident.
        try:
            preview_img = page.query_selector(
                '[data-testid="media-viewer-image"], div[role="dialog"] img, '
                '#app div[data-animate-modal-body="true"] img'
            )
            if preview_img:
                preview_img.click()
        except Exception:
            pass

        # Defensive: if the sticker toggle is sitting in an active/pressed
        # state (from a stray earlier focus/keypress), click it back off
        # before we do anything else. We only ever act on it to disarm it.
        try:
            for sel in _STICKER_TOGGLE_SELECTORS:
                toggle = page.query_selector(sel)
                if toggle and (_safe_get_attr(toggle, "aria-pressed") == "true"
                               or "selected" in (_safe_get_attr(toggle, "class") or "")):
                    toggle.click()
                    page.wait_for_timeout(150)
                break
        except Exception:
            pass

        if caption.strip():
            caption_box = None
            for sel in _COMPOSE_SELECTORS + ['div[aria-label="Add a caption"][contenteditable="true"]']:
                try:
                    el = page.query_selector(sel)
                    if el:
                        caption_box = el
                        break
                except Exception:
                    continue
            if caption_box:
                try:
                    caption_box.click()
                    page.keyboard.type(caption.strip(), delay=15)
                    page.wait_for_timeout(200)
                except Exception:
                    pass  # send without caption rather than failing the whole attach

        send_btn = None
        for sel in _MEDIA_SEND_SELECTORS:
            try:
                # Give WA a moment to render the button rather than judging
                # off a single instantaneous snapshot.
                btn = page.wait_for_selector(sel, timeout=2_000)
                if btn:
                    send_btn = btn
                    break
            except Exception:
                continue

        if not send_btn:
            # No blind Enter fallback: we don't know what currently has
            # focus, and pressing Enter near the sticker toggle is exactly
            # how a photo turns into a sticker send. Fail closed instead —
            # process_outbox() will requeue/retry through MAX_RETRIES.
            print("  [dom_send_file] send button not found — not guessing, aborting this attempt")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return False

        send_btn.click()
        page.wait_for_timeout(1_500)  # let the upload complete before moving on
        return True

    except Exception as e:
        print(f"  [dom_send_file] error: {e}")
        return False


_SAFE_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9]{1,5}$")
_CONTENT_TYPE_SUFFIX = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/webp": ".webp", "image/gif": ".gif", "application/pdf": ".pdf",
}


def _suffix_for(url: str, content_type: str = "") -> str:
    """Extension for the temp file. Uses only the URL *path* (never the query
    string) and only accepts a plain alphanumeric extension — Airbnb CDN
    URLs look like '.../abc.jpeg?im_w=720', and Path(url).suffix on that
    returned '.jpeg?im_w=720', which Windows rejects with [Errno 22] Invalid
    argument the moment mkstemp tried to create the file. Falls back to the
    Content-Type header, then '.bin'."""
    try:
        from urllib.parse import urlparse
        ext = Path(urlparse(url).path).suffix.lower()
    except Exception:
        ext = ""
    if ext == ".jpeg":
        ext = ".jpg"
    if _SAFE_SUFFIX_RE.match(ext):
        return ext
    ct = (content_type or "").split(";")[0].strip().lower()
    return _CONTENT_TYPE_SUFFIX.get(ct, ".bin")


def download_to_temp(url: str, suffix: str = "") -> Optional[str]:
    """Fetches a server-hosted file (e.g. /static/invoices/xyz.pdf or
    /static/houses/<key>/1.jpg) — or any absolute http(s) URL — to a local
    temp path so Playwright's set_input_files has a real filesystem path to
    hand WhatsApp. Returns None on failure."""
    try:
        full_url = url if url.startswith("http") else f"{KARLON_URL}{url}"
        r = get_session().get(full_url, timeout=DOWNLOAD_TIMEOUT)
        r.raise_for_status()
        if not r.content:
            raise RuntimeError("empty body")
        if not suffix:
            suffix = _suffix_for(url, r.headers.get("content-type", ""))
        fd, tmp_path = tempfile.mkstemp(prefix="karlon_", suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(r.content)
        return tmp_path
    except Exception as e:
        print(f"  [download_to_temp] failed for {url}: {e}")
        return None


# ─────────────────────────────── outbox poller thread ─────────────────────────

def _outbox_poller(stop_event: threading.Event) -> None:
    """
    Dedicated background thread — the only REST calls this thread makes are:
      GET  /api/outbox           → fetch pending messages
    It does NOT touch Playwright; all browser ops stay on the main thread.

    Items are deduplicated by message-id so we never put the same message
    into _outbox twice, even if it comes back on successive polls before the
    main thread has processed it.
    """
    print("[outbox-thread] started — polling every %ds" % OUTBOX_POLL_SEC)
    while not stop_event.is_set():
        try:
            r = get_session().get(f"{KARLON_URL}/api/outbox", timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            items = r.json()
            if items:
                print(f"[outbox-thread] {len(items)} pending item(s) from server")
            with _ids_lock:
                for item in items:
                    mid = item.get("id", "")
                    if mid and mid not in _queued_ids:
                        _queued_ids.add(mid)
                        item.setdefault("_retries", 0)
                        _outbox.put_nowait(item)
        except Exception as e:
            print(f"[outbox-thread] poll error: {e}")
        stop_event.wait(timeout=OUTBOX_POLL_SEC)
    print("[outbox-thread] stopped")

# ─────────────────────── urgent outbox poller thread ─────────────────────────

def _urgent_outbox_poller(stop_event: threading.Event) -> None:
    """
    Dedicated background thread for urgent customer replies.

    Polls GET /api/outbox/urgent every URGENT_OUTBOX_POLL_SEC (2 s).
    Items have wa_status='urgent' — set by POST /api/reply/{chat_id} when
    a staff member sends a time-sensitive reply from the Karlon app.

    Uses its own queue (_urgent_outbox) and dedup set (_urgent_queued_ids)
    so urgent messages never get mixed into or blocked by the normal
    5-second pending queue. process_outbox() drains this queue first.

    Ack path is identical: after DOM delivery the main thread calls
      PATCH /api/chats/{chat_id}/messages/{msg_id}/wa-ack?status=sent
    which sets wa_status='sent' and removes the item from this queue.
    """
    print(f"[urgent-outbox-thread] started — polling every {URGENT_OUTBOX_POLL_SEC}s")
    while not stop_event.is_set():
        try:
            r = get_session().get(f"{KARLON_URL}/api/outbox/urgent", timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            items = r.json()
            if items:
                print(f"[urgent-outbox-thread] {len(items)} urgent reply(s) from server")
            with _urgent_ids_lock:
                for item in items:
                    mid = item.get("id", "")
                    if mid and mid not in _urgent_queued_ids:
                        _urgent_queued_ids.add(mid)
                        item.setdefault("_retries", 0)
                        item["_urgent"] = True   # flag so process_outbox can log it
                        _urgent_outbox.put_nowait(item)
        except Exception as e:
            print(f"[urgent-outbox-thread] poll error: {e}")
        stop_event.wait(timeout=URGENT_OUTBOX_POLL_SEC)
    print("[urgent-outbox-thread] stopped")


# ─────────────────────────────── process outbox (main thread) ─────────────────

def outbox_pending() -> bool:
    """True if either queue has work. Both must be checked: process_outbox()
    drains urgent first, but callers used to gate it on `not _outbox.empty()`
    alone — so an urgent reply arriving mid-sync sat unsent, however quickly
    its own 2 s poller had fetched it, until the *normal* queue happened to
    fill or the whole pass over every chat finished."""
    return not (_urgent_outbox.empty() and _outbox.empty())


def idle_wait(page: Page, seconds: float) -> None:
    """Sleep `seconds`, but wake every 0.5 s to deliver anything that arrives.
    Replaces a flat time.sleep(POLL_INTERVAL) during which outbound messages
    just waited (Little's law, L = λW: added dwell time is added queue length;
    here the "service" is instant once the browser is free, so the fix is to
    be free — not to poll faster)."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if outbox_pending():
            process_outbox(page)
        time.sleep(0.5)


def process_outbox(page: Page) -> None:
    """
    Drain the outbox queues and DOM-send each pending message to WhatsApp.
    Must be called from the main Playwright thread.

    Priority order:
      1. _urgent_outbox  — customer replies (wa_status='urgent', polled every 2 s)
      2. _outbox         — normal messages  (wa_status='pending', polled every 5 s)

    Urgent messages are always processed first so a staff member's reply
    to a guest is never delayed by broadcast or bulk traffic in the normal queue.
    Only the individual message text is delivered — not the full chat history;
    each outbox item carries exactly: text, wa_name, chat_id, kind, media_url.
    """
    # ── 1. Drain urgent replies first ─────────────────────────────────────────
    urgent: list[dict] = []
    while not _urgent_outbox.empty():
        try:
            urgent.append(_urgent_outbox.get_nowait())
        except queue.Empty:
            break

    if urgent:
        print(f"[outbox] ⚡ {len(urgent)} URGENT reply(s) — processing first...")
        _process_items(page, urgent, queue_name="urgent", ids_lock=_urgent_ids_lock, ids_set=_urgent_queued_ids)

    # ── 2. Then drain normal pending queue ────────────────────────────────────
    if _outbox.empty():
        return

    pending: list[dict] = []
    while not _outbox.empty():
        try:
            pending.append(_outbox.get_nowait())
        except queue.Empty:
            break

    if not pending:
        return

    print(f"[outbox] processing {len(pending)} outbound message(s)...")
    _process_items(page, pending, queue_name="outbox", ids_lock=_ids_lock, ids_set=_queued_ids)


def _process_items(
    page: Page,
    items: list[dict],
    queue_name: str,
    ids_lock: threading.Lock,
    ids_set: set,
) -> None:
    """
    Core send loop shared by both outbox queues (urgent + normal).

    Each `item` is one outbound message — exactly the fields the server
    returned from GET /api/outbox or GET /api/outbox/urgent:
      text, wa_name, chat_id, kind, media_url, id
    There is no chat history context here — just the single message to deliver.
    """
    for item in items:
        chat_id    = item.get("chat_id", "")
        msg_id     = item.get("id", "")
        text       = (item.get("text") or "").strip()
        wa_name    = (item.get("wa_name") or "").strip()
        retries    = item.get("_retries", 0)
        chat_name  = item.get("chat_name", wa_name or chat_id)
        kind       = item.get("kind", "text")
        media_url  = item.get("media_url")

        if not wa_name:
            print(f"  [outbox] no wa_name for chat '{chat_name}' — marking error")
            _ack_wa_message(chat_id, msg_id, "error")
            with ids_lock:
                ids_set.discard(msg_id)
            continue

        if not text and not media_url:
            print(f"  [outbox] empty message {msg_id} — nothing to send, marking error")
            _ack_wa_message(chat_id, msg_id, "error")
            with ids_lock:
                ids_set.discard(msg_id)
            continue

        # ──────────────────────────────────────────────────────────────────────────
        # NOTE: outbound images (Airbnb listing photos from houses.py::send_listings,
        # or a staff upload from messages.py::send_image) are legitimate and must be
        # allowed through. The old blanket "kind == image -> block" guard that used
        # to live here was guarding against scraped *inbound* photos getting echoed
        # back to the contact they came from — but that path is already closed off
        # upstream: import_image_message() (messages.py, POST /images/import) stores
        # scraped images with wa_status=NULL, so they never reach wa_status='pending'
        # and never show up in GET /api/outbox in the first place. Any 'image' item
        # that does arrive here got here because a chat/staff member genuinely sent
        # it — same as a text message — so it goes through the normal send path
        # below rather than being auto-failed.
        # ──────────────────────────────────────────────────────────────────────────

        # Echo guard: refuse anything that matches text we recently scraped
        # as INBOUND, on any chat. This is a content-integrity problem, not
        # a transient one — retrying won't fix it, so fail straight to
        # error instead of requeuing.
        if text and _looks_like_inbound_echo(text):
            print(
                f"  [outbox] text for msg {msg_id} matches a recently-seen "
                f"inbound message — refusing to send (possible echo-back), marking error"
            )
            _ack_wa_message(chat_id, msg_id, "error")
            with ids_lock:
                ids_set.discard(msg_id)
            continue

        # Duplicate-blast guard: refuse to send identical text to a second,
        # different recipient within the cooldown window. One legitimate
        # reply is fine; the same line firing at multiple numbers back to
        # back is the exact shape of unsolicited bulk messaging.
        if text and _looks_like_duplicate_blast(text, wa_name):
            print(
                f"  [outbox] identical text already sent to a different recipient "
                f"in the last {_OUTBOUND_DUPLICATE_WINDOW_SEC // 60} min — "
                f"refusing to send to '{wa_name}' too, marking error for manual review"
            )
            _ack_wa_message(chat_id, msg_id, "error")
            with ids_lock:
                ids_set.discard(msg_id)
            continue

        # Profile-picture guard: refuse any media_url that's obviously a
        # contact's own avatar rather than a real attachment (invoice PDF,
        # listing photo). sync_once() stores avatars under
        # /static/profile_pics/<chat>.jpg — if that same path shows up as
        # an outbox item's media_url, something upstream mixed up "this
        # chat's photo" with "a file to send".
        if media_url and _looks_like_profile_pic(media_url):
            print(
                f"  [outbox] media_url for msg {msg_id} looks like a profile "
                f"picture ('{media_url}') — refusing to send it back to the contact, marking error"
            )
            _ack_wa_message(chat_id, msg_id, "error")
            with ids_lock:
                ids_set.discard(msg_id)
            continue

        def _requeue_or_fail(reason: str) -> None:
            print(f"  [outbox] {reason} (attempt {retries + 1}/{MAX_RETRIES})")
            if retries + 1 >= MAX_RETRIES:
                print(f"  [outbox] giving up on msg {msg_id}")
                _ack_wa_message(chat_id, msg_id, "error")
                with ids_lock:
                    ids_set.discard(msg_id)
            else:
                item["_retries"] = retries + 1
                # Back onto the queue it came from: an urgent reply that hit a
                # transient failure used to be demoted into the normal queue,
                # losing its priority exactly when it most needed a retry.
                (_urgent_outbox if queue_name == "urgent" else _outbox).put_nowait(item)
                # Don't discard from ids_set — keeps it in the queue

        # Open the WhatsApp chat by clicking the correct row in the sidebar
        if not open_chat_row(page, wa_name):
            _requeue_or_fail(f"could not open WA chat '{wa_name}'")
            continue

        # Identity gate: re-read who the open conversation actually is from
        # WA's own header, independent of the sidebar click that got us
        # here, and refuse to send on a mismatch instead of trusting it.
        open_title = get_open_chat_title(page)
        if open_title and open_title.strip() != wa_name.strip():
            _requeue_or_fail(
                f"open chat is '{open_title}', expected '{wa_name}' — refusing to send"
            )
            continue

        if media_url:
            # Images and documents (e.g. generated invoice PDFs) both go
            # through the attach flow — fetch the server-hosted file to a
            # local temp path first since Playwright needs a real path.
            local_path = download_to_temp(media_url)
            if not local_path:
                _requeue_or_fail(f"could not download media for msg {msg_id}")
                continue
            try:
                sent = dom_send_file(page, local_path, caption=text, kind=kind)
            finally:
                try:
                    os.remove(local_path)
                except OSError:
                    pass
        else:
            sent = dom_send_message(page, text)

        if sent:
            if text:
                _record_outbound_send(text, wa_name)
            label = text[:60] if text else f"[{kind}]"
            print(f"  [outbox] ✓ sent to '{wa_name}': {label}")
            _ack_wa_message(chat_id, msg_id, "sent")
        else:
            _requeue_or_fail(f"dom_send failed for '{wa_name}'")

# ──────────────────────── everything below is unchanged from original ──────────
# (selector discovery, profile pics, message scraping, sync loop, main)
# ──────────────────────────────────────────────────────────────────────────────

def discover_row_selector(page: Page) -> Optional[str]:
    for sel in _ROW_SELECTOR_CANDIDATES:
        try:
            rows = page.query_selector_all(sel)
            if rows:
                print(f"  [discover] Row selector match: {sel!r}  → {len(rows)} rows")
                return sel
            else:
                print(f"  [discover] {sel!r} → 0 rows")
        except Exception as e:
            print(f"  [discover] {sel!r} → error: {e}")
    return None

def discover_title_selector(page: Page, row_sel: str) -> Optional[str]:
    try:
        row = page.query_selector(row_sel)
        if not row:
            return None
        for sel in _TITLE_SELECTOR_CANDIDATES:
            el = row.query_selector(sel)
            if el:
                text = _safe_inner_text(el) or _safe_get_attr(el, "title")
                if text:
                    print(f"  [discover] Title selector: {sel!r} → {text!r}")
                    return sel
    except Exception:
        pass
    return None

def dump_selectors(page: Page) -> None:
    print("\n=== Selector Discovery ===")
    try:
        items = page.evaluate("""() => {
            const pane = document.querySelector('#pane-side') ||
                         document.querySelector('[data-testid="chat-list"]');
            if (!pane) return ['pane not found'];
            const els = pane.querySelectorAll('[data-testid]');
            const seen = {};
            els.forEach(el => {
                const dt = el.getAttribute('data-testid');
                seen[dt] = (seen[dt] || 0) + 1;
            });
            return Object.entries(seen).map(([k,v]) => k + ' ×' + v);
        }""")
        for item in (items or []):
            print(f"  {item}")
    except Exception as e:
        print(f"  evaluate failed: {e}")
    print("=== End Discovery ===\n")

def fetch_blob_b64(page: Page, src: str) -> Optional[str]:
    try:
        result = page.evaluate(
            """async (src) => {
                try {
                    const r = await fetch(src);
                    const blob = await r.blob();
                    if (!blob || blob.size === 0) return { error: `empty blob` };
                    const b64 = await new Promise(resolve => {
                        const reader = new FileReader();
                        reader.onload = () => resolve(reader.result.split(',')[1]);
                        reader.readAsDataURL(blob);
                    });
                    return { data: b64 };
                } catch(e) { return { error: String(e) }; }
            }""",
            src,
        )
        if isinstance(result, dict) and result.get("data"):
            return result["data"]
        return None
    except Exception:
        return None

def save_profile_pic(chat_id: str, b64_data: str) -> str:
    PROFILE_PICS_DIR.mkdir(parents=True, exist_ok=True)
    (PROFILE_PICS_DIR / f"{chat_id}.jpg").write_bytes(base64.b64decode(b64_data))
    return f"/static/profile_pics/{chat_id}.jpg"

def get_profile_photo_b64(page: Page, debug: bool = False) -> Optional[str]:
    header_el = page.query_selector("#main header")
    if not header_el:
        return None
    try:
        header_el.hover()
        page.wait_for_timeout(400)
    except Exception:
        pass
    click_target = (
        page.query_selector('#main header >> text=contact info')
        or page.query_selector('#main header span[data-testid="default-user"]')
        or page.query_selector('#main header [role="button"]')
        or header_el
    )
    try:
        click_target.click(force=True)
    except Exception:
        return None
    try:
        page.wait_for_selector("text=Contact info, text=Group info", timeout=8_000)
    except Exception:
        pass
    page.wait_for_timeout(800)
    if page.query_selector('text=Business information') or page.query_selector('text=Business name'):
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return None
    b64: Optional[str] = None
    try:
        viewport = page.viewport_size or {"width": 1400, "height": 900}
        right_edge_threshold = viewport["width"] * 0.65
        imgs = page.query_selector_all("img[src]")
        best_img = None
        best_area = 0.0
        for img in imgs:
            try:
                src_attr = _safe_get_attr(img, "src")
                if not src_attr or src_attr.startswith("data:image/svg"):
                    continue
                box = img.bounding_box()
                if not box or box["x"] < right_edge_threshold:
                    continue
                area = box["width"] * box["height"]
                if area > best_area:
                    best_area = area
                    best_img = img
            except Exception:
                continue
        if best_img and best_area > 3_000:
            src = _safe_get_attr(best_img, "src") or None
            if src:
                b64 = fetch_blob_b64(page, src)
            if not b64:
                try:
                    png_bytes = best_img.screenshot()
                    b64 = base64.b64encode(png_bytes).decode("ascii")
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        try:
            close_btn = page.query_selector('button[aria-label="Close"], [data-testid="x-viewer"]')
            if close_btn:
                close_btn.click()
            else:
                page.keyboard.press("Escape")
        except Exception:
            pass
    return b64

def launch_page(headless: bool = False):
    pw = sync_playwright().start()
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=SESSION_DIR,
        headless=headless,
        viewport={"width": 1400, "height": 900},
    )
    page = ctx.new_page()
    try:
        page.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=60_000)
    except Exception as e:
        print(f"[wa_bridge] goto warning: {e} — retrying once...")
        page.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=60_000)

    qr_sel = (
        'canvas[aria-label="Scan this QR code to link a device!"],'
        ' div[data-testid="qrcode"]'
    )
    list_sel = _CHATLIST_CONTAINER

    print(f"[wa_bridge] Browser: {'HEADLESS' if headless else 'HEADED (visible)'}")
    print("Waiting for WhatsApp to load...")
    try:
        page.wait_for_selector(f"{qr_sel}, {list_sel}", timeout=20_000)
    except Exception:
        pass
    if page.query_selector(qr_sel):
        print("QR shown — scan it within 5 minutes...")
        page.wait_for_selector(list_sel, timeout=5 * 60 * 1000)
        print("Scanned — logged in.")
    else:
        print("Session active — waiting for chat list...")
        page.wait_for_selector(list_sel, timeout=30_000)
        print("Logged in.")
    print("Letting chat list populate (3 s)...")
    time.sleep(3)
    return pw, ctx, page

def _resolve_row_selector(page: Page) -> str:
    for sel in _ROW_SELECTOR_CANDIDATES:
        try:
            if page.query_selector(sel):
                return sel
        except Exception:
            continue
    return '#pane-side [role="listitem"]'

def _get_name_from_row(row, page: Page) -> str:
    for sel in _TITLE_SELECTOR_CANDIDATES:
        try:
            el = row.query_selector(sel)
            if not el:
                continue
            name = _safe_get_attr(el, "title")
            if not name:
                name = _safe_inner_text(el)
            if name:
                return name
        except Exception:
            continue
    return ""

def _get_preview_from_row(row) -> str:
    for sel in _PREVIEW_SELECTOR_CANDIDATES:
        try:
            el = row.query_selector(sel)
            if el:
                text = _safe_inner_text(el)
                if text:
                    return text
        except Exception:
            continue
    return ""

def _wait_for_rows(page: Page, timeout_s: int = 15) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for sel in _ROW_SELECTOR_CANDIDATES:
            try:
                if page.query_selector(sel):
                    return True
            except Exception:
                pass
        time.sleep(0.5)
    return False

def get_all_chat_rows(page: Page) -> list[dict]:
    """
    Scroll through the ENTIRE sidebar and return every chat row found.

    WhatsApp Web uses virtual scrolling — only ~20-30 rows are in the DOM
    at any time. A plain query_selector_all() therefore only sees the top
    of the list. This function drives the pane-side div downward in steps,
    collecting rows as they enter the viewport and deduplicating by name,
    until three consecutive scroll steps yield no new contacts.

    All unsaved numbers (every contact whose display name is a phone number)
    are included regardless of unread count or keywords — this is the fix
    for sync only seeing a small subset of contacts.
    """
    if not _wait_for_rows(page, timeout_s=10):
        print("  [warn] No chat rows after 10 s — WA might still be loading")
        return []

    pane = None
    for sel in ("#pane-side", '[data-testid="chat-list"]', '[aria-label*="Chat list"]'):
        try:
            pane = page.query_selector(sel)
            if pane:
                break
        except Exception:
            continue

    # Scroll to top first so we start from the beginning of the list
    if pane:
        try:
            pane.evaluate("el => el.scrollTop = 0")
            time.sleep(0.4)
        except Exception:
            pass

    seen_names: set[str] = set()
    all_rows:   list[dict] = []
    no_new_streak = 0

    while no_new_streak < 3:
        batch = get_chat_rows(page, skip_names=seen_names)
        new_this_pass = 0
        for row in batch:
            name = row.get("name", "")
            if name and name not in seen_names:
                seen_names.add(name)
                all_rows.append(row)
                new_this_pass += 1

        if new_this_pass == 0:
            no_new_streak += 1
        else:
            no_new_streak = 0

        # Scroll the sidebar down by one screen-height of rows
        if pane:
            try:
                pane.evaluate("el => el.scrollTop += 900")
                time.sleep(0.6)           # let WA render the newly visible rows
            except Exception:
                break
        else:
            break                         # no pane handle — can't scroll, stop

    return all_rows


def get_chat_rows(page: Page, skip_names: Optional[set] = None) -> list[dict]:
    """Visible sidebar rows. `skip_names` lets the full-list sweep avoid
    re-reading rows it already captured: the virtual list keeps ~25 rows in the
    DOM and each 900 px scroll step overlaps the last, so every row used to be
    re-extracted (4-5 DOM calls each) several times per pass."""
    if not _wait_for_rows(page, timeout_s=10):
        print("  [warn] No chat rows found after 10 s — WA might still be loading")
    row_sel = _resolve_row_selector(page)
    out = []
    try:
        rows = page.query_selector_all(row_sel)
    except Exception:
        return out
    for row in rows:
        try:
            name = _get_name_from_row(row, page)
            if not name or is_wa_section_header(name):
                continue
            if skip_names and name in skip_names:
                continue
            preview = _get_preview_from_row(row)
            fingerprint = hashlib.sha1(_safe_inner_text(row).encode("utf-8", "replace")).hexdigest()
            unread = 0
            try:
                badge = row.query_selector('span[aria-label*="unread"], [data-testid*="unread"]')
                if badge:
                    m = re.search(r"\d+", _safe_get_attr(badge, "aria-label"))
                    if not m:
                        m = re.search(r"\d+", _safe_inner_text(badge))
                    if m:
                        unread = int(m.group())
            except Exception:
                pass
            pic_src: Optional[str] = None
            try:
                img = row.query_selector('img[src^="blob:"]')
                if img:
                    pic_src = _safe_get_attr(img, "src")
            except Exception:
                pass
            out.append({"name": name, "preview": preview, "unread": unread,
                        "pic_src": pic_src or None, "fingerprint": fingerprint})
        except Exception:
            continue
    return out

def get_open_chat_title(page: Page) -> Optional[str]:
    """
    Read back the title WhatsApp is *actually* showing in the open
    conversation header. Used as an identity gate right after
    open_chat_row() so the tool never sends on the strength of "a click
    landed somewhere" — it re-derives who the open chat really is from
    the DOM and compares that against the intended recipient before any
    send is allowed to proceed. Full DOM access, hard restriction to intent.

    The header's outer clickable wrapper carries a generic instructional
    title ("Click here for contact info" / "...for group info") and is a
    span too, so it matches before the real name span nested inside it —
    every candidate is checked against a denylist of that boilerplate
    rather than trusting the first title-bearing span found.
    """
    try:
        for el in page.query_selector_all('#main header span[title]'):
            title = (_safe_get_attr(el, "title") or "").strip()
            if title and title.lower() not in _HEADER_TITLE_DENYLIST:
                return title
        # Fallback: any header span with real text, same denylist filter.
        for el in page.query_selector_all('#main header span[dir="auto"]'):
            text = (_safe_inner_text(el) or "").strip()
            if text and text.lower() not in _HEADER_TITLE_DENYLIST:
                return text
    except Exception:
        pass
    return None


def should_sync(chat: dict, sync_all: bool) -> bool:
    if sync_all:
        return True
    if is_unsaved_number(chat["name"]):
        return True
    name, preview = chat["name"].lower(), chat["preview"].lower()
    if any(kw in name or kw in preview for kw in FLAG_KEYWORDS):
        return True
    if any(chat["name"].strip().startswith(p) for p in FLAG_NAME_PREFIXES):
        return True
    if chat["unread"] > 0:
        return True
    return False

def _find_and_click_row(page: Page, chat_name: str) -> bool:
    """One attempt against whatever's currently rendered in the DOM."""
    row_sel = _resolve_row_selector(page)
    try:
        rows = page.query_selector_all(row_sel)
    except Exception:
        return False
    for row in rows:
        try:
            name = _get_name_from_row(row, page)
            if not name or name != chat_name.strip():
                continue
            try:
                row.scroll_into_view_if_needed(timeout=2_000)
            except Exception:
                pass
            try:
                row.click(position={"x": 5, "y": 5})
            except Exception:
                return False
            try:
                page.wait_for_selector(
                    '[data-testid="conversation-panel-messages"],'
                    ' [data-testid="msg-container"],'
                    ' #main [role="row"]',
                    timeout=15_000,
                )
            except Exception:
                return False
            time.sleep(0.8)
            return True
        except Exception:
            continue
    return False


def _clear_sidebar_search(page: Page) -> None:
    """Best-effort: get the sidebar back to the full chat list after a search."""
    for sel in _SEARCH_CLEAR_SELECTORS:
        try:
            btn = page.query_selector(sel)
            if btn:
                btn.click()
                time.sleep(0.3)
                return
        except Exception:
            continue
    # Fallback: focus whatever search input is open and blow it away with Escape.
    try:
        page.keyboard.press("Escape")
        time.sleep(0.3)
    except Exception:
        pass


def _search_and_click_row(page: Page, chat_name: str) -> bool:
    """
    Use WA's own sidebar search to surface `chat_name` instead of scrolling
    the virtualized list by hand — search results render immediately
    regardless of where #pane-side happens to be scrolled, so this is both
    faster and more reliable than the scroll-hunt fallback below. Tried
    first; scrolling only kicks in if search comes up empty (ambiguous
    match, WA UI quirk, etc.).
    """
    search_box = None
    for sel in _SEARCH_INPUT_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                search_box = el
                break
        except Exception:
            continue

    if not search_box:
        # Newer WA sometimes needs the search icon clicked before the input
        # exists in the DOM at all — same two-step pattern as the attach menu.
        for sel in _SEARCH_BUTTON_SELECTORS:
            try:
                btn = page.query_selector(sel)
                if btn:
                    btn.click()
                    time.sleep(0.3)
                    break
            except Exception:
                continue
        for sel in _SEARCH_INPUT_SELECTORS:
            try:
                el = page.query_selector(sel)
                if el:
                    search_box = el
                    break
            except Exception:
                continue

    if not search_box:
        return False

    try:
        search_box.click()
        page.keyboard.press("Control+a")
        page.keyboard.press("Delete")
        page.keyboard.type(chat_name, delay=15)
        time.sleep(0.6)  # let WA's own filtering settle
    except Exception:
        _clear_sidebar_search(page)
        return False

    found = _find_and_click_row(page, chat_name)
    _clear_sidebar_search(page)
    return found


def open_chat_row(page: Page, chat_name: str) -> bool:
    """
    Find `chat_name` and open it.

    WhatsApp virtualizes #pane-side — only ~20-30 rows are ever in the DOM,
    centered on wherever the pane is currently scrolled to. get_all_chat_rows()
    leaves the pane scrolled to the bottom after its full-list sweep, so a
    plain query_selector_all() only ever finds rows near that resting
    position — everything else silently fails to open. Three-step lookup,
    cheapest/most-reliable first:
      1. current DOM (handles whatever's already visible, no-op cost)
      2. WA's own sidebar search (jumps straight to the row regardless of
         scroll position — this is what a human would do)
      3. manual scroll-hunt from the top, same steps get_all_chat_rows uses,
         as a last resort if search misses for some reason
    """
    if _find_and_click_row(page, chat_name):
        return True

    if _search_and_click_row(page, chat_name):
        return True

    pane = None
    for sel in ("#pane-side", '[data-testid="chat-list"]', '[aria-label*="Chat list"]'):
        try:
            pane = page.query_selector(sel)
            if pane:
                break
        except Exception:
            continue
    if not pane:
        return False

    try:
        pane.evaluate("el => el.scrollTop = 0")
        time.sleep(0.4)
    except Exception:
        return False

    if _find_and_click_row(page, chat_name):
        return True

    no_new_streak = 0
    while no_new_streak < 3:
        try:
            pane.evaluate("el => el.scrollTop += 900")
            time.sleep(0.6)
        except Exception:
            break
        if _find_and_click_row(page, chat_name):
            return True
        no_new_streak += 1  # scrollTop clamps at the bottom, so this also caps the loop

    return False

def parse_wa_timestamp(raw: str) -> Optional[str]:
    for fmt in ("%H:%M, %d/%m/%Y", "%I:%M %p, %m/%d/%Y", "%H:%M, %m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).isoformat()
        except ValueError:
            continue
    return None

# One browser round-trip per message: direction AND the WhatsApp message id.
# (detect_direction used to cost a CDP call per message and the data-id needed
# for de-duplication would have cost a second one.) Signals are tried in order
# of how well each has survived WhatsApp's DOM changes; the first that answers
# wins, and `via` records which one for debugging.
_PROBE_JS = """el => {
    const msgRow = el.closest('[data-id]') || el;
    const dataId = (msgRow.getAttribute && msgRow.getAttribute('data-id')) || '';
    const done = (direction, via) => ({ direction, via, dataId });

    // 1. accessibility label on the sender span: "You:" vs "<contact>:"
    const ariaSpan = msgRow.querySelector('span[aria-label$=":"]');
    if (ariaSpan) {
        const label = ariaSpan.getAttribute('aria-label') || '';
        if (label === 'You:') return done('out', 'aria');
        if (label.endsWith(':')) return done('in', 'aria');
    }

    // 2. bubble tail icon (first message of a consecutive group only)
    const tail = msgRow.querySelector('[data-testid="tail-out"], [data-testid="tail-in"], [data-icon="tail-out"], [data-icon="tail-in"]');
    if (tail) {
        const t = (tail.getAttribute('data-testid') || tail.getAttribute('data-icon') || '');
        if (t.includes('tail-out')) return done('out', 'tail');
        if (t.includes('tail-in'))  return done('in', 'tail');
    }

    // 3. legacy class names
    const cls = msgRow.className || '';
    if (/\\bmessage-out\\b/.test(cls)) return done('out', 'class');
    if (/\\bmessage-in\\b/.test(cls))  return done('in', 'class');

    // 4. WhatsApp's own message key: "true_<jid>_<id>" = sent by this account
    if (dataId.startsWith('true_'))  return done('out', 'data-id');
    if (dataId.startsWith('false_')) return done('in', 'data-id');

    // 5. geometry — the WhatsApp layout itself. Outgoing bubbles sit in the
    // right half of the message pane, incoming in the left. Independent of
    // any class name or attribute, so it still answers when WA reshuffles its
    // markup. A dead-centre bubble (within 5% of the pane midpoint) is
    // refused rather than guessed.
    const pane = el.closest('[data-testid="conversation-panel-messages"], [role="application"], #main');
    if (pane) {
        const p = pane.getBoundingClientRect(), r = el.getBoundingClientRect();
        if (p.width > 0 && r.width > 0) {
            const offset = ((r.left + r.width / 2) - (p.left + p.width / 2)) / p.width;
            if (offset >  0.05) return done('out', 'geometry');
            if (offset < -0.05) return done('in',  'geometry');
        }
    }
    return done('unknown', 'none');
}"""


def probe_message(el) -> dict:
    """{'direction': 'in'|'out'|'unknown', 'via': str, 'dataId': str} for one
    message element, in a single browser round-trip."""
    try:
        res = el.evaluate(_PROBE_JS)
        if isinstance(res, dict):
            return res
    except Exception:
        pass
    return {"direction": "unknown", "via": "error", "dataId": ""}


def detect_direction(row) -> str:
    """'out' = sent by you, 'in' = sent by the other party, 'unknown' = no
    signal answered. Kept as a thin wrapper for callers that only need the
    direction; scrape paths use probe_message() + resolve_directions()."""
    return probe_message(row)["direction"]


def resolve_directions(entries: list[dict]) -> None:
    """Make every entry's 'direction' exactly 'in' or 'out', in place.

    The app draws a message on the right if direction == 'out' and on the left
    otherwise, so the layout is only as persistent as this field is
    deterministic. An 'unknown' used to be shipped to the server as-is, and
    whatever the client did with it (usually: left) put some of *your*
    messages on the wrong side, differently on different screens.

    Resolution, strongest evidence first, all from the same scrape:
      1. the same raw sender was classified elsewhere in this chat
         (majority vote — your own display name always maps to 'out');
      2. otherwise 'in' — the safe default, since a contact's message on the
         left is the WhatsApp norm and 'out' needs positive evidence.
    Each entry needs 'raw_sender' (the name WhatsApp printed, '' if unknown).
    """
    votes: dict[str, dict[str, int]] = {}
    for e in entries:
        if e["direction"] in ("in", "out") and e.get("raw_sender"):
            v = votes.setdefault(e["raw_sender"], {"in": 0, "out": 0})
            v[e["direction"]] += 1
    for e in entries:
        if e["direction"] in ("in", "out"):
            continue
        v = votes.get(e.get("raw_sender") or "")
        if v and v["out"] != v["in"]:
            e["direction"] = "out" if v["out"] > v["in"] else "in"
        else:
            e["direction"] = "in"

def scrape_image_messages(page: Page, chat_name: str) -> list[dict]:
    out = []
    try:
        rows = page.query_selector_all(
            '[data-testid="conversation-panel-messages"] [data-id],'
            ' #main [role="application"] [data-id]'
        )
    except Exception:
        return out
    for row in rows:
        try:
            data_id = _safe_get_attr(row, "data-id")
            if not data_id:
                continue
            img_el = None
            for candidate in row.query_selector_all("img[src]"):
                src_attr = _safe_get_attr(candidate, "src")
                if not src_attr or src_attr.startswith("data:image/svg"):
                    continue
                box = candidate.bounding_box()
                if box and box["width"] * box["height"] > 2_000:
                    img_el = candidate
                    break
            if not img_el:
                continue
            caption_el = row.query_selector("[data-pre-plain-text]")
            pre_plain = _safe_get_attr(caption_el, "data-pre-plain-text") if caption_el else ""
            caption = _safe_inner_text(caption_el) if caption_el else ""
            if pre_plain and caption.startswith(pre_plain):
                caption = caption[len(pre_plain):].strip()
            m = re.match(r"\[(.*?)\]\s*(.*?):\s*$", pre_plain) if pre_plain else None
            ts_raw, sender = (m.group(1), m.group(2)) if m else ("", "")
            direction = probe_message(row)["direction"]
            src = _safe_get_attr(img_el, "src")
            b64 = fetch_blob_b64(page, src) if src else None
            if not b64:
                try:
                    png_bytes = img_el.screenshot()
                    b64 = base64.b64encode(png_bytes).decode("ascii")
                except Exception:
                    continue
            out.append({
                "raw_sender":   sender,
                "caption":      caption,
                "created_at":   parse_wa_timestamp(ts_raw),
                "external_key": hashlib.sha1(f"img|{data_id}".encode()).hexdigest(),
                "b64":          b64,
                "direction":    direction,
            })
        except Exception:
            continue
    resolve_directions(out)
    for e in out:
        e["sender"] = "You (WhatsApp)" if e["direction"] == "out" else (e["raw_sender"] or chat_name)
    return out

def save_incoming_image(b64_data: str) -> Path:
    INCOMING_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{hashlib.sha1(b64_data[:256].encode()).hexdigest()}.jpg"
    path = INCOMING_IMAGES_DIR / fname
    path.write_bytes(base64.b64decode(b64_data))
    return path

def import_image_message(
    chat_id: str, sender: str, caption: str, image_path: Path,
    external_key: str, direction: str = "in",
) -> Optional[dict]:
    """
    Posts to /images/import (the wa_bridge-scraped path, dedup'd by
    external_key), NOT /images (the app/staff-composed path). This used to
    hit /images, which is documented server-side as hardcoding
    direction='out' + wa_status='pending' — meaning every image scraped off
    WhatsApp, incoming or outgoing, went in flagged as a pending outbound
    send. The outbox poller would then pick it straight back up and DOM-send
    it back to WhatsApp: every image ever scraped by wa_bridge was getting
    echoed back into the chat it came from. /images/import is the endpoint
    that was actually built to prevent exactly this (dedup + direction='in'
    + wa_status=NULL, so the outbox never touches it) — it just wasn't
    being called.
    """
    try:
        with open(image_path, "rb") as f:
            r = get_session().post(
                f"{KARLON_URL}/api/chats/{chat_id}/images/import",
                data={
                    "sender": sender,
                    "caption": caption,
                    "external_key": external_key,
                    "direction": direction,
                },
                files={"file": (image_path.name, f, "image/jpeg")},
                timeout=UPLOAD_TIMEOUT,
            )
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        print(f"    ! image upload to Karlon failed: {e}")
        return None

def scrape_messages(page: Page, chat_name: str, max_scroll_rounds: int = 12) -> list[dict]:
    """
    Scroll up and collect every message currently loadable in this chat.

    Used to scroll a fixed 4 rounds regardless of chat length, which meant
    long-running chats only ever contributed their most recent slice —
    everything older just never made it into Karlon. Scrolling to the top
    is now adaptive: keep hitting scrollTop=0 (which triggers WA to lazy-load
    the next older batch) until the topmost rendered message stops changing
    for two rounds running — that's the actual top of history, not a guess —
    or `max_scroll_rounds` is hit as a safety cap so one enormous chat can't
    stall the whole sync cycle. Pass a larger `max_scroll_rounds` (e.g. via
    --deep-history) for a genuine one-time full-history clone of every chat.
    """
    panel = page.query_selector(
        '[data-testid="conversation-panel-messages"], #main [role="application"]'
    )
    if not panel:
        return []

    row_probe_sel = (
        '[data-testid="conversation-panel-messages"] [data-id],'
        ' #main [role="application"] [data-id]'
    )
    prev_top_id: Optional[str] = None
    stall = 0
    for _ in range(max_scroll_rounds):
        try:
            panel.evaluate("el => el.scrollTop = 0")
        except Exception:
            break
        time.sleep(0.5)
        try:
            top_id = page.eval_on_selector(row_probe_sel, "el => el.getAttribute('data-id')")
        except Exception:
            top_id = None
        if top_id is not None and top_id == prev_top_id:
            stall += 1
            if stall >= 2:
                break
        else:
            stall = 0
        prev_top_id = top_id

    out = []
    seen_keys: set[str] = set()
    covered_ids: set[str] = set()   # WA data-ids the timestamped pass below already emitted

    try:
        meta_nodes = page.query_selector_all(
            '[data-testid="conversation-panel-messages"] [data-pre-plain-text],'
            ' #main [data-pre-plain-text]'
        )
    except Exception:
        meta_nodes = []

    for meta_el in meta_nodes:
        try:
            pre_plain = _safe_get_attr(meta_el, "data-pre-plain-text")
            m = re.match(r"\[(.*?)\]\s*(.*?):\s*$", pre_plain)
            ts_raw, sender = (m.group(1), m.group(2)) if m else ("", "")
            text = _safe_inner_text(meta_el)
            if pre_plain and text.startswith(pre_plain):
                text = text[len(pre_plain):].strip()
            if not text:
                continue
            text = reformat_ad_context_card(text)
            probe = probe_message(meta_el)
            if probe["dataId"]:
                covered_ids.add(probe["dataId"])
            # NOTE: external_key formulas below are the persisted identity of
            # every message already in Karlon — do not change them without a
            # server-side migration or the whole history re-imports as new.
            ext_key = hashlib.sha1(f"{pre_plain}|{text}".encode()).hexdigest()
            seen_keys.add(ext_key)
            out.append({
                "raw_sender":   sender,
                "text":         text,
                "created_at":   parse_wa_timestamp(ts_raw),
                "external_key": ext_key,
                "direction":    probe["direction"],
            })
        except Exception:
            continue

    try:
        rows = page.query_selector_all(
            '[data-testid="conversation-panel-messages"] [data-id],'
            ' #main [role="application"] [data-id]'
        )
    except Exception:
        rows = []

    for row in rows:
        try:
            data_id = _safe_get_attr(row, "data-id")
            if not data_id:
                continue
            if data_id in covered_ids:
                # Already exported (with its timestamp and real sender) by the
                # data-pre-plain-text pass. This pass keys on "row|<id>|<text>",
                # which can never match that pass's key, so without this skip
                # every text message was emitted twice.
                continue
            has_real_image = any(
                (candidate.bounding_box() or {"width": 0, "height": 0})["width"]
                * (candidate.bounding_box() or {"width": 0, "height": 0})["height"] > 2_000
                for candidate in row.query_selector_all("img[src]")
                if not _safe_get_attr(candidate, "src").startswith("data:image/svg")
            )
            if has_real_image:
                continue
            text_el = row.query_selector(
                "span.selectable-text, div.copyable-text span, "
                "span[dir='ltr'], span[dir='auto']"
            )
            if not text_el:
                continue
            text = _safe_inner_text(text_el)
            if not text:
                continue
            text = reformat_ad_context_card(text)
            ext_key = hashlib.sha1(f"row|{data_id}|{text}".encode()).hexdigest()
            if ext_key in seen_keys:
                continue
            seen_keys.add(ext_key)
            out.append({
                "raw_sender":   "",
                "text":         text,
                "created_at":   None,
                "external_key": ext_key,
                "direction":    probe_message(row)["direction"],
            })
        except Exception:
            continue

    # Every message leaves here with direction exactly 'in' or 'out' — the
    # contract the app's left/right bubble layout depends on.
    resolve_directions(out)
    for e in out:
        raw = e.pop("raw_sender", "")
        e["sender"] = "You (WhatsApp)" if e["direction"] == "out" else (raw or chat_name)
    return out

# ─────────────────────────────── sync loop ────────────────────────────────────

_imported_image_keys: set[str] = set()

# ── Outbound safety nets ────────────────────────────────────────────────
# These exist because the tool nearly caused real harm: identical text
# went out to two different numbers back-to-back (a spam pattern that can
# get a WA Business number banned), and there's no local way to prove an
# outbound item isn't actually inbound content looping back. Both checks
# fail CLOSED — refuse and flag for a human, never guess and send anyway.

_recent_inbound_texts: dict[str, float] = {}   # normalized text -> last-seen ts
_INBOUND_ECHO_WINDOW_SEC = 15 * 60             # 15 min

_recent_outbound_sends: list[tuple[float, str, str]] = []  # (ts, norm text, wa_name)
_OUTBOUND_DUPLICATE_WINDOW_SEC = 10 * 60       # 10 min


def _normalize_text(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip().lower())


def _record_inbound_text(text: str) -> None:
    norm = _normalize_text(text)
    if not norm:
        return
    now = time.time()
    _recent_inbound_texts[norm] = now
    cutoff = now - _INBOUND_ECHO_WINDOW_SEC
    for k in [k for k, ts in _recent_inbound_texts.items() if ts < cutoff]:
        del _recent_inbound_texts[k]


def _looks_like_inbound_echo(text: str) -> bool:
    norm = _normalize_text(text)
    if not norm:
        return False
    ts = _recent_inbound_texts.get(norm)
    return ts is not None and (time.time() - ts) <= _INBOUND_ECHO_WINDOW_SEC


def _looks_like_duplicate_blast(text: str, wa_name: str) -> bool:
    norm = _normalize_text(text)
    if not norm:
        return False
    now = time.time()
    global _recent_outbound_sends
    _recent_outbound_sends = [
        (ts, t, n) for (ts, t, n) in _recent_outbound_sends
        if now - ts <= _OUTBOUND_DUPLICATE_WINDOW_SEC
    ]
    return any(t == norm and n != wa_name for (_, t, n) in _recent_outbound_sends)


def _record_outbound_send(text: str, wa_name: str) -> None:
    norm = _normalize_text(text)
    if norm:
        _recent_outbound_sends.append((time.time(), norm, wa_name))


def _looks_like_profile_pic(media_url: str) -> bool:
    """
    A legitimate outbound attachment is an invoice PDF or a listing photo —
    never a contact's own avatar. sync_once() saves those to
    /static/profile_pics/<chat>.jpg and posts that path to the server as
    profile_pic_url; if an outbox item's media_url ever points at that same
    path, something upstream conflated "this chat's photo" with "a file to
    send" — refuse it rather than mailing someone their own picture back.
    """
    low = (media_url or "").lower()
    return "profile_pic" in low


# Incremental sync. Every pass used to open and scroll every chat (~2-6 s
# each) whether or not anything had happened in it. The sidebar row's text —
# name, last-message time, preview, unread badge — changes whenever a chat
# has new activity, so an unchanged fingerprint means "nothing new to read".
# A fingerprint is only recorded after a chat imported cleanly, and every
# WA_FULL_SWEEP_EVERY-th pass ignores fingerprints entirely, so a message the
# fingerprint failed to reflect (identical text, same minute) is picked up at
# most that many passes later. Same idea as version vectors (Parker et al.,
# UCLA, 1983): cheap per-replica summary to detect divergence, full
# reconciliation only where the summaries differ — plus a periodic
# anti-entropy sweep as the backstop.
_chat_fingerprints: dict[str, str] = {}
_sync_pass = 0


def sync_once(page: Page, sync_all: bool, fetch_pics: bool, max_scroll_rounds: int = 12) -> None:
    global _sync_pass
    _sync_pass += 1
    full_sweep = (_sync_pass - 1) % WA_FULL_SWEEP_EVERY == 0
    # Scroll the entire sidebar so ALL contacts are collected, not just the
    # ~20 rows WhatsApp's virtual scroll keeps visible at any given time.
    chats   = get_all_chat_rows(page)
    targets = [c for c in chats if should_sync(c, sync_all)]
    unsaved = [c for c in targets if is_unsaved_number(c["name"])]
    saved   = [c for c in targets if not is_unsaved_number(c["name"])]
    skipped = 0
    print(
        f"{len(chats)} chats found (full scroll) — syncing {len(targets)} "
        f"({len(unsaved)} unsaved numbers, {len(saved)} saved contacts)"
        f"{'  [full sweep]' if full_sweep else ''}"
    )
    pics_fetched = 0
    for c in targets:
        # Outbound (messages / invoices) always jumps the queue ahead of
        # continuing to walk the chat list — check before every chat, not
        # just once the whole sync pass finishes.
        if outbox_pending():
            process_outbox(page)
        fp = c.get("fingerprint")
        if not full_sweep and fp and _chat_fingerprints.get(c["name"]) == fp:
            skipped += 1
            continue
        try:
            cid = ensure_chat(c["name"])
        except requests.RequestException as e:
            print(f"  ! Karlon unreachable after retries ({e}) — check {KARLON_URL}, skipping {c['name']!r} this pass")
            continue
        if not open_chat_row(page, c["name"]):
            print(f"  ! could not open: {c['name']}")
            continue
        pic_url: Optional[str] = None
        if fetch_pics:
            b64 = get_profile_photo_b64(page, debug=False)
            if b64:
                try:
                    pic_url = save_profile_pic(chat_slug(c["name"]), b64)
                    pics_fetched += 1
                    ensure_chat(c["name"], pic_url=pic_url)
                except Exception as e:
                    print(f"    ! pic save failed for {c['name']}: {e}")
        msgs   = scrape_messages(page, c["name"], max_scroll_rounds=max_scroll_rounds)
        for m in msgs:
            if m.get("sender") != "You (WhatsApp)" and m.get("text"):
                _record_inbound_text(m["text"])
        result = import_messages(cid, msgs)
        images_sent = 0
        for img in scrape_image_messages(page, c["name"]):
            ext_key = img["external_key"]
            if ext_key in _imported_image_keys:
                continue
            path = save_incoming_image(img["b64"])
            uploaded = import_image_message(
                cid, img["sender"], img["caption"], path,
                external_key=ext_key, direction=img.get("direction", "in"),
            )
            try:
                path.unlink()
            except OSError:
                pass
            if uploaded:
                _imported_image_keys.add(ext_key)
                images_sent += 1
        label   = "\U0001f4f1 " if is_unsaved_number(c["name"]) else "   "
        pic_tag = " \U0001f5bc" if pic_url else ""
        img_tag = f" \U0001f4f7+{images_sent}" if images_sent else ""
        print(
            f"  {label}{c['name']}: "
            f"+{result['imported']} new (skipped {result['skipped']}){pic_tag}{img_tag}"
        )
        if c.get("fingerprint"):
            _chat_fingerprints[c["name"]] = c["fingerprint"]
    if skipped:
        print(f"  ({skipped} chat(s) unchanged since last pass — not re-read)")
    if fetch_pics:
        print(f"  Got {pics_fetched} profile picture(s)")


# ─────────────────────────────── entry point ──────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once",     action="store_true", help="sync one pass then exit")
    ap.add_argument("--all",      action="store_true", help="sync every chat")
    ap.add_argument("--pics",     action="store_true", help="download profile pictures")
    ap.add_argument("--headless", action="store_true", default=False, help="hide browser")
    ap.add_argument("--discover", action="store_true", help="dump selector info and exit")
    ap.add_argument(
        "--deep-history",
        action="store_true",
        help="scroll each chat much further back before importing (use with --once --all "
             "for a one-time full clone; slower per-chat, don't leave this on for the "
             "routine 30s loop)",
    )
    args = ap.parse_args()

    print_active_config()

    try:
        get_session().get(KARLON_URL, timeout=(5, 20))
    except requests.RequestException:
        print(f"! Can't reach Karlon at {KARLON_URL}. Start it first:")
        print("    uvicorn app.main:app --host 0.0.0.0 --port 8000")
        return

    pw, ctx, page = launch_page(headless=args.headless)

    # ── Start outbox-poller threads ──────────────────────────────────────────
    stop_event = threading.Event()

    # Normal pending queue — polls GET /api/outbox every OUTBOX_POLL_SEC (5 s)
    poller = threading.Thread(
        target=_outbox_poller,
        args=(stop_event,),
        name="outbox-poller",
        daemon=True,
    )
    poller.start()

    # Urgent reply queue — polls GET /api/outbox/urgent every 2 s, FIRST priority.
    # Only receives messages sent via POST /api/reply/{chat_id} (wa_status='urgent').
    # Delivers exactly the individual message text — no chat history context.
    urgent_poller = threading.Thread(
        target=_urgent_outbox_poller,
        args=(stop_event,),
        name="urgent-outbox-poller",
        daemon=True,
    )
    urgent_poller.start()

    try:
        if args.discover:
            dump_selectors(page)
            print("\nRow selector scan:")
            discover_row_selector(page)
            return

        while True:
            # 1. Import new messages from WhatsApp → Karlon
            sync_once(
                page,
                sync_all=args.all,
                fetch_pics=args.pics,
                max_scroll_rounds=60 if args.deep_history else 12,
            )

            # 2. Process outbound queue: send Karlon messages → WhatsApp via DOM
            process_outbox(page)

            if args.once:
                break
            print(f"Idle {POLL_INTERVAL}s (delivering outbound as it arrives)...  "
                  f"(outbox thread polls every {OUTBOX_POLL_SEC}s)\n")
            idle_wait(page, POLL_INTERVAL)

    finally:
        stop_event.set()
        poller.join(timeout=3)
        urgent_poller.join(timeout=3)
        ctx.close()
        pw.stop()


if __name__ == "__main__":
    main()