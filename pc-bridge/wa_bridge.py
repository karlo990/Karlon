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
    python wa_bridge.py --chat "Karl" --dry-run    # read ONE chat, write chat_snapshots/<id>.json, send nothing
    python wa_bridge.py --chat "Karl"              # sync one chat (applies its snapshot on the server)
    python wa_bridge.py                            # continuous: the 15 most recent chats (WA_SYNC_LAST_N_CHATS)
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
import json
import queue
import re
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import os
import requests
from playwright.sync_api import Page, sync_playwright

# ─────────────────────────────────── config ───────────────────────────────────

from karlon_client import DOWNLOAD_TIMEOUT, HTTP_TIMEOUT, UPLOAD_TIMEOUT, get_session
from local_config import (
    KARLON_URL, WA_POLL_INTERVAL, WA_OUTBOX_POLL_SEC, WA_FULL_SWEEP_EVERY,
    WA_SYNC_LAST_N_CHATS, CHAT_SNAPSHOT_DIR, WA_UTC_OFFSET_MINUTES, WA_RELOAD_HOURS,
    print_active_config,
)
from wa_clean import (
    chat_slug, clean_chat_name, combine, detect_dayfirst, is_system_notice,
    is_time_only, is_unread_divider, parse_divider_date, parse_pre_plain,
    parse_pre_timestamp, split_trailing_time,
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
# "Photos & videos" first. WhatsApp's attach menu also has a STICKER input
# that accepts image/* — picking it sent house photos as stickers. Only the
# photos input also accepts video, so that is what identifies it.
_IMAGE_INPUT_SELECTORS = [
    'input[type="file"][accept*="video"][accept*="image"]',
    'input[type="file"][accept*="video"]',
    'input[type="file"][accept*="image"]:not([accept="image/webp"])',
    'input[type="file"][accept*="image"]',
]

# Send button inside the media/document preview modal (distinct DOM from
# the plain-text compose send button above, though WA sometimes reuses it).
_MEDIA_SEND_SELECTORS = [
    '[data-icon="wds-ic-send-filled"]',
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

        attach_btn.click(timeout=5_000)
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

        # Defensive: if the sticker toggle is sitting in an active/pressed
        # state (from a stray earlier focus/keypress), click it back off
        # before we do anything else. We only ever act on it to disarm it.
        try:
            for sel in _STICKER_TOGGLE_SELECTORS:
                toggle = page.query_selector(sel)
                if toggle and (_safe_get_attr(toggle, "aria-pressed") == "true"
                               or "selected" in (_safe_get_attr(toggle, "class") or "")):
                    toggle.click(timeout=3_000)
                    page.wait_for_timeout(150)
                break
        except Exception:
            pass

        # Caption: only into a text box that is ON TOP of the screen, i.e. the
        # editor's own caption field. The first compose-box selector used to
        # match the chat's message box BEHIND the photo editor: the caption
        # went there, which made that hidden send button appear, and every
        # click on it was then blocked by the editor ("... intercepts pointer
        # events") for 30 s per attempt.
        if caption.strip():
            caption_box = _topmost(page, _CAPTION_SELECTORS)
            if caption_box:
                try:
                    _click_center(page, caption_box)
                    page.keyboard.type(caption.strip(), delay=15)
                    page.wait_for_timeout(200)
                except Exception:
                    pass  # send without caption rather than failing the whole attach

        send_btn = None
        deadline = time.time() + 4
        while send_btn is None and time.time() < deadline:
            send_btn = _topmost(page, _MEDIA_SEND_SELECTORS)
            if send_btn is None:
                page.wait_for_timeout(250)

        if not send_btn:
            # No blind Enter fallback: we don't know what currently has
            # focus, and pressing Enter near the sticker toggle is exactly
            # how a photo turns into a sticker send. Fail closed instead —
            # process_outbox() will requeue/retry through MAX_RETRIES.
            print("  [dom_send_file] no send button on top of the editor — not guessing, aborting this attempt")
            _dismiss_media_editor(page)
            return False

        _click_center(page, send_btn)
        # Sent = the editor (and its send button) went away.
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                if not send_btn.evaluate("e => e.isConnected && e.getBoundingClientRect().width > 0"):
                    page.wait_for_timeout(800)   # let the upload get going before moving on
                    return True
            except Exception:
                return True                     # handle detached: editor closed
            page.wait_for_timeout(300)
        print("  [dom_send_file] editor still open after clicking send — treating as not sent")
        _dismiss_media_editor(page)
        return False

    except Exception as e:
        print(f"  [dom_send_file] error: {str(e).splitlines()[0][:200]}")
        _dismiss_media_editor(page)
        return False


# The photo/document editor's caption field, best match first. Every match is
# also required to be the top-most element at its own centre (see _topmost).
_CAPTION_SELECTORS = [
    'div[aria-label="Add a caption"][contenteditable="true"]',
    'div[aria-label*="caption" i][contenteditable="true"]',
    'div[contenteditable="true"][role="textbox"]',
    'div[contenteditable="true"]',
]

_TOPMOST_JS = """(sels) => {
    const vis = e => { const r = e.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0
            && r.top < innerHeight && r.left < innerWidth; };
    for (const sel of sels) {
        let els;
        try { els = Array.from(document.querySelectorAll(sel)).filter(vis).reverse(); }
        catch (e) { continue; }
        for (const e of els) {
            const r = e.getBoundingClientRect();
            const t = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
            if (!t) continue;
            if (t === e || e.contains(t)) return e;
            if (t.contains(e)) {                  // a small wrapper, e.g. the button around an icon
                const tr = t.getBoundingClientRect();
                if (tr.width <= 3 * r.width + 40 && tr.height <= 3 * r.height + 40) return e;
            }
        }
    }
    return null;
}"""


def _topmost(page: Page, selectors: list[str]):
    """First visible element matching `selectors` that is actually on top at
    its own centre — i.e. what a person would hit by clicking there. Elements
    hidden behind a dialog (the chat's own send button behind the photo
    editor) are skipped instead of being clicked until timeout."""
    try:
        return page.evaluate_handle(_TOPMOST_JS, selectors).as_element()
    except Exception:
        return None


def _click_center(page: Page, el) -> None:
    box = el.bounding_box()
    if not box:
        raise RuntimeError("element has no box")
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


def _dismiss_media_editor(page: Page) -> None:
    """Close a photo/document editor left open by a failed send, so the next
    attempt (or the next chat) doesn't start inside it. Escape, then confirm
    WhatsApp's "discard?" prompt if it asks."""
    for _ in range(2):
        if _topmost(page, _MEDIA_SEND_SELECTORS) is None:
            return
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
            for btn in page.query_selector_all('div[role="dialog"] button, [role="dialog"] div[role="button"]'):
                if re.search(r"discard", _safe_inner_text(btn), re.IGNORECASE):
                    btn.click(timeout=3_000)
                    page.wait_for_timeout(300)
                    break
        except Exception:
            return


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


def _image_format(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return "other"


def ensure_jpeg(page: Page, path: str) -> str:
    """Photos are sent as real JPEG photos. WhatsApp treats a WebP file (what
    image CDNs often serve, whatever the URL says) as a sticker, so anything
    that isn't JPEG/PNG is re-encoded to a .jpg, judged by the file's bytes,
    not its name. Pillow if installed, else the browser's own decoder.
    Returns the path to send (the original if no conversion was needed or
    possible)."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return path
    fmt = _image_format(data)
    if fmt in ("jpeg", "png"):
        if fmt == "jpeg" and not path.lower().endswith((".jpg", ".jpeg")):
            new = str(Path(path).with_suffix(".jpg"))
            os.replace(path, new)
            return new
        return path
    jpg: Optional[bytes] = None
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(data)).convert("RGB")
        buf = BytesIO()
        img.save(buf, "JPEG", quality=90)
        jpg = buf.getvalue()
    except Exception:
        try:
            b64 = page.evaluate(
                """async (src) => {
                    const img = new Image(); img.src = src; await img.decode();
                    const c = document.createElement('canvas');
                    c.width = img.naturalWidth; c.height = img.naturalHeight;
                    const g = c.getContext('2d'); g.fillStyle = '#fff'; g.fillRect(0, 0, c.width, c.height);
                    g.drawImage(img, 0, 0);
                    return c.toDataURL('image/jpeg', 0.9).split(',')[1];
                }""",
                f"data:image/{fmt if fmt != 'other' else 'webp'};base64," + base64.b64encode(data).decode("ascii"),
            )
            jpg = base64.b64decode(b64) if b64 else None
        except Exception as e:
            print(f"  [ensure_jpeg] could not convert {fmt} image: {str(e).splitlines()[0][:120]}")
    if not jpg:
        return path
    new = str(Path(path).with_suffix(".jpg"))
    Path(new).write_bytes(jpg)
    if new != path:
        try:
            os.remove(path)
        except OSError:
            pass
    return new


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
        # clean_chat_name: chats created by older builds stored names like
        # "1 unread message\nKarl", which match no sidebar row and no header.
        wa_name    = clean_chat_name(item.get("wa_name")) or (item.get("wa_name") or "").strip()
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
        if header_shows(page, wa_name) is False:
            _requeue_or_fail(
                f"open chat is '{_short(get_open_chat_title(page))}', expected '{wa_name}' — refusing to send"
            )
            continue

        if media_url:
            # Images and documents (e.g. generated invoice PDFs) both go
            # through the attach flow — fetch the server-hosted file to a
            # local temp path first since Playwright needs a real path.
            local_path = download_to_temp(media_url)
            if local_path and kind == "image":
                local_path = ensure_jpeg(page, local_path)
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
    print(dom_report(page))
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

def reload_whatsapp(page: Page) -> None:
    """Fresh WhatsApp Web page (see WA_RELOAD_HOURS). The session is kept on
    disk, so no QR scan is needed; if the chat list doesn't come back within a
    minute the loop just carries on and the next pass retries."""
    print("[wa_bridge] reloading WhatsApp Web to free memory...")
    try:
        page.reload(wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_selector(_CHATLIST_CONTAINER, timeout=60_000)
        time.sleep(3)
        print("[wa_bridge] reloaded.")
    except Exception as e:
        print(f"[wa_bridge] reload incomplete ({e}) — continuing")


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

def _get_name_from_row(row, page: Optional[Page] = None) -> str:
    """The contact/group name in a sidebar row, cleaned (see
    wa_clean.clean_chat_name). The first candidate used to be returned as-is,
    and on current WhatsApp Web that was "1 unread message\nKarl" — the
    screen-reader unread label plus the name — which created one Karlon chat
    per unread count instead of one "Karl"."""
    for sel in _TITLE_SELECTOR_CANDIDATES:
        try:
            els = row.query_selector_all(sel)
        except Exception:
            continue
        for el in els:
            for raw in (_safe_get_attr(el, "title"), _safe_inner_text(el)):
                name = clean_chat_name(raw)
                if name:
                    return name
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


def get_recent_chat_rows(page: Page, limit: int) -> list[dict]:
    """The `limit` most recent chats, in WhatsApp's own sidebar order (most
    recent activity first; pinned chats first, as WhatsApp shows them).

    The sidebar is a virtual list whose DOM order is not guaranteed to match
    what's on screen, so rows are ranked by their position in the scrolled
    list, not by DOM index. Scrolls only as far as needed to see `limit` rows.
    """
    if limit <= 0 or not _wait_for_rows(page, timeout_s=10):
        return []
    pane = None
    for sel in ("#pane-side", '[data-testid="chat-list"]', '[aria-label*="Chat list"]'):
        try:
            pane = page.query_selector(sel)
            if pane:
                break
        except Exception:
            continue
    if pane:
        try:
            pane.evaluate("el => el.scrollTop = 0")
            time.sleep(0.4)
        except Exception:
            pass

    best: dict[str, dict] = {}
    stall = 0
    while True:
        offset = 0.0
        if pane:
            try:
                box = pane.bounding_box() or {"y": 0}
                offset = pane.evaluate("el => el.scrollTop") - box["y"]
            except Exception:
                pass
        new = 0
        for r in get_chat_rows(page, skip_names=set(best), with_position=True):
            r["abs_y"] = r.pop("_y", 0.0) + offset
            best[r["name"]] = r
            new += 1
        stall = 0 if new else stall + 1
        if len(best) >= limit + 2 or stall >= 2 or not pane:
            break
        try:
            pane.evaluate("el => el.scrollTop += 600")
            time.sleep(0.5)
        except Exception:
            break
    rows = sorted(best.values(), key=lambda r: r["abs_y"])[:limit]
    for r in rows:
        r.pop("abs_y", None)
    return rows


def get_chat_rows(page: Page, skip_names: Optional[set] = None,
                  with_position: bool = False) -> list[dict]:
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
            entry = {"name": name, "preview": preview, "unread": unread,
                     "pic_src": pic_src or None, "fingerprint": fingerprint}
            if with_position:
                box = row.bounding_box()
                if not box:
                    continue
                entry["_y"] = box["y"]
            out.append(entry)
        except Exception:
            continue
    return out

_HEADER_NAMES_JS = """() => {
    const h = document.querySelector('#main header');
    if (!h) return [];
    const out = [];
    // The chat's own name: current WhatsApp puts it in a span with NO title
    // attribute (data-testid="conversation-info-header"); the only span WITH
    // a title is a group's member list ("Anele, Class, …, You").
    for (const e of h.querySelectorAll('[data-testid="conversation-info-header"] span[dir="auto"], span[data-testid="conversation-info-header"], span[dir="auto"]'))
        out.push((e.innerText || e.textContent || '').trim());
    for (const e of h.querySelectorAll('[title]')) out.push(e.getAttribute('title') || '');
    return out;
}"""


def get_open_chat_names(page: Page) -> list[str]:
    """Every name-like string in the open conversation's header, cleaned —
    the chat's name plus whatever else WhatsApp shows there (a group's member
    list, "Profile details"). Identity checks ask whether the expected name is
    among them, rather than trusting whichever string comes first."""
    try:
        raw = page.evaluate(_HEADER_NAMES_JS) or []
    except Exception:
        return []
    names: list[str] = []
    for r in raw:
        n = clean_chat_name(r)
        if n and n.lower() not in _HEADER_TITLE_DENYLIST and n.lower() != "profile details" and n not in names:
            names.append(n)
    return names


def get_open_chat_title(page: Page) -> Optional[str]:
    """The open chat's name as WhatsApp's header shows it (first candidate),
    or None if the header can't be read. Prefer header_shows() for identity
    checks — a group header also contains its member list."""
    names = get_open_chat_names(page)
    return names[0] if names else None


def _name_key(name: str) -> str:
    """Letters and digits only, case-folded. The sidebar's title attribute
    keeps emoji ("ROYAL CREST ACADEMY ADVOCATES 🎓") but the header's text
    drops them (WhatsApp draws emoji as images), so an exact comparison
    refused chats that were open and correct."""
    return "".join(ch for ch in (name or "").casefold() if ch.isalnum())


def header_shows(page: Page, name: str) -> Optional[bool]:
    """True/False: does the open conversation's header show `name`?
    None: no header name readable at all."""
    names = get_open_chat_names(page)
    if not names:
        return None
    want = _name_key(clean_chat_name(name) or name)
    return bool(want) and any(_name_key(n) == want for n in names)


def _short(s: Optional[str], n: int = 60) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + "…"


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
            if _wait_chat_open(page, chat_name, timeout_s=8):
                return True
            # Second try on the row's centre (the corner can land on padding
            # or the avatar, depending on WhatsApp's layout).
            try:
                row.click()
            except Exception:
                return False
            return _wait_chat_open(page, chat_name, timeout_s=6)
        except Exception:
            continue
    return False


def _wait_chat_open(page: Page, chat_name: str, timeout_s: float) -> bool:
    """True once the conversation header shows `chat_name`.

    This used to wait for [data-testid="conversation-panel-messages"] (or
    msg-container / #main [role="row"]). Current WhatsApp Web no longer
    renders those, so every chat "could not open" even though the click had
    opened it. The header name is the check that matters anyway — it is the
    same identity gate outbound sends use. Only if no header name can be read
    at all does a visible message list count as open."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if header_shows(page, chat_name):
            time.sleep(0.6)            # let the message list render
            return True
        time.sleep(0.4)
    if header_shows(page, chat_name) is None:
        try:
            state = page.evaluate(_OPEN_STATE_JS)
            if state and state.get("msgs"):
                return True
        except Exception:
            pass
    _report_dom_once(page, f"couldn't confirm '{chat_name}' opened "
                           f"(header shows {_short(get_open_chat_title(page))!r})")
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

# Direction signals for one message element, tried in order of how well each
# has survived WhatsApp's DOM changes; the first that answers wins and `via`
# records which one. Shared by probe_message() and the whole-chat walk below.
_PROBE_FN = """function probe(el, geoEl) {
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

    // 3. message-out / message-in class on the row, inside it or around it
    if (msgRow.closest('.message-out') || msgRow.querySelector('.message-out')) return done('out', 'class');
    if (msgRow.closest('.message-in')  || msgRow.querySelector('.message-in'))  return done('in', 'class');

    // 4. WhatsApp's own message key: "true_<jid>_<id>" = sent by this account
    //    (older builds; current ones use a bare id)
    if (dataId.startsWith('true_'))  return done('out', 'data-id');
    if (dataId.startsWith('false_')) return done('in', 'data-id');

    // 5. delivery ticks / pending clock — WhatsApp draws these only on
    //    messages YOU sent, photos included
    const tick = Array.from(msgRow.querySelectorAll('[data-icon], [data-testid]')).find(i => {
        const k = (i.getAttribute('data-icon') || '') + ' ' + (i.getAttribute('data-testid') || '');
        return /(dblcheck|msg-check|status-check|msg-time|status-time)/.test(k);
    });
    if (tick) return done('out', 'ticks');

    // 6. geometry of the bubble CONTENT (text block or photo) — outgoing sits
    //    right of the pane's centre, incoming left. The row element itself can
    //    span the full width, so it is not measured. Dead centre is refused.
    const pane = el.closest('[data-testid="conversation-panel-messages"], [role="application"], #main');
    const target = geoEl || el;
    if (pane) {
        const p = pane.getBoundingClientRect(), r = target.getBoundingClientRect();
        if (p.width > 0 && r.width > 0) {
            const offset = ((r.left + r.width / 2) - (p.left + p.width / 2)) / p.width;
            if (offset >  0.05) return done('out', 'geometry');
            if (offset < -0.05) return done('in',  'geometry');
        }
    }
    return done('unknown', 'none');
}"""

_PROBE_JS = "el => { " + _PROBE_FN + " return probe(el); }"

# The open conversation's scrollable message list. Known markers first; else
# the nearest scrolling ancestor of a message ([data-id]) inside #main —
# which keeps working when WhatsApp drops its data-testid / role markers.
_PANEL_FN = """function findPanel() {
    const known = document.querySelector('[data-testid="conversation-panel-messages"]')
               || document.querySelector('#main [role="application"]');
    if (known) return known;
    const scope = document.querySelector('#main') || document;
    const msg = Array.from(scope.querySelectorAll('[data-id]'))
        .find(m => !m.closest('#pane-side, [aria-label*="Chat list"]'));
    if (!msg) return null;
    for (let el = msg.parentElement; el && el !== document.body; el = el.parentElement) {
        const oy = getComputedStyle(el).overflowY;
        if ((oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight) return el;
    }
    return msg.closest('#main') || msg.parentElement;
}"""

_OPEN_STATE_JS = ("() => { " + _PANEL_FN +
                  " const p = findPanel(); return { msgs: p ? p.querySelectorAll('[data-id]').length : 0 }; }")

# One-call map of the WhatsApp page: which of the markers this bridge relies
# on exist, the header names, and the attribute skeleton of the first sidebar
# row and first message (tags + attributes only, values cut to 24 chars).
_DOM_REPORT_JS = """() => {
    const sels = ['#main', '#main header', '#main header span[title]', '#main footer',
        '[data-testid="conversation-panel-messages"]', '#main [role="application"]',
        '#main [role="row"]', '#main [data-id]', '[data-pre-plain-text]',
        '#pane-side', '#pane-side [role="listitem"]', '#pane-side [role="row"]',
        '[role="grid"]', '[data-testid="cell-frame-container"]',
        '[data-testid="cell-frame-title"]', 'span[title][dir="auto"]'];
    const counts = {};
    for (const s of sels) { try { counts[s] = document.querySelectorAll(s).length; } catch (e) { counts[s] = 'err'; } }
    const ATTR = ['role', 'data-testid', 'data-id', 'data-icon', 'aria-label', 'title',
                  'data-pre-plain-text', 'tabindex', 'dir', 'id'];
    function skel(el, d) {
        if (!el || d > 6) return '';
        const a = [];
        for (const n of ATTR) { const v = el.getAttribute && el.getAttribute(n);
            if (v !== null && v !== undefined) a.push(n + '="' + String(v).slice(0, 24) + '"'); }
        const kids = Array.from(el.children || []).slice(0, 4).map(c => skel(c, d + 1)).filter(Boolean);
        return '  '.repeat(d) + '<' + el.tagName.toLowerCase() + (a.length ? ' ' + a.join(' ') : '') + '>'
             + (kids.length ? '\\n' + kids.join('\\n') : '');
    }
    const headerTitles = Array.from(document.querySelectorAll('#main header [title]'))
        .slice(0, 5).map(e => e.getAttribute('title'));
    const row = document.querySelector('#pane-side [role="listitem"], #pane-side [role="row"], [data-testid="cell-frame-container"]');
    const msg = document.querySelector('#main [data-id]');
    return { counts, headerTitles, row: skel(row, 0), header: skel(document.querySelector('#main header'), 0),
             msg: skel(msg, 0) };
}"""

_dom_reported = False


def dom_report(page: Page) -> str:
    try:
        r = page.evaluate(_DOM_REPORT_JS)
    except Exception as e:
        return f"[dom-report] failed: {e}"
    lines = ["[dom-report] ---- WhatsApp page structure (send this if chats won't sync) ----"]
    lines += [f"[dom-report] {k:48} {v}" for k, v in r["counts"].items()]
    lines.append(f"[dom-report] header titles: {r['headerTitles']}")
    for part in ("row", "header", "msg"):
        lines.append(f"[dom-report] first {part}:")
        lines += [f"[dom-report]   {l}" for l in (r[part] or "(none)").split("\n")[:40]]
    lines.append("[dom-report] ---- end ----")
    return "\n".join(lines)


def _report_dom_once(page: Page, reason: str) -> None:
    global _dom_reported
    if _dom_reported:
        return
    _dom_reported = True
    print(f"  ! {reason}")
    print(dom_report(page))


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


def resolve_directions(entries: list[dict]) -> None:
    """Make every entry's 'direction' exactly 'in' or 'out', in place.

    The app draws a message on the right if direction == 'out' and on the left
    otherwise, so the layout is only as stable as this field. Unresolved
    entries take the majority direction of the same raw sender elsewhere in the
    chat, else 'in' ('out' needs positive evidence). Each entry needs
    'raw_sender' (the name WhatsApp printed, '' if unknown)."""
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


# ─────────────────────────── whole-chat scan → one snapshot ────────────────
#
# A chat is read in ONE browser round-trip: _WALK_JS returns every row of the
# open conversation — messages and the dividers between them — in on-screen
# order, with the raw facts about each (WhatsApp message id, the
# "[time, date] sender:" metadata, text, the bubble's own time label, image
# presence, direction). build_chat_snapshot() then turns that into a single
# validated JSON document for the chat. It never guesses silently: every row is
# either a message in the snapshot or listed under "rejected" with a reason.
#
# Ordering. The on-screen order IS the conversation order, so it is kept as
# the source of truth, and timestamps are derived to agree with it:
#   * "[20:16, 29/09/2026] Karl:" metadata when present (exact to the minute);
#   * otherwise the bubble's own time label on the date of the nearest date
#     divider above it ("TODAY", "Yesterday", "Monday", "29/09/2026");
#   * otherwise inherited from the neighbouring message (marked "inferred").
# Old builds sent no time at all for photos and for messages without metadata,
# so the server stamped them with the *import* time and they sank to the
# bottom of the chat — the "04:38" photo in the screenshots.
# Times are then made non-decreasing in screen order, and messages that share a
# minute get +1 ms each, so "ORDER BY created_at" reproduces the screen exactly.
# Everything is sent in UTC; old builds sent naive local time, which sorted
# two hours off against app-sent messages (stored in UTC).

_WALK_JS = """() => {
    """ + _PROBE_FN + _PANEL_FN + """
    const panel = findPanel();
    if (!panel) return null;
    const TIME = /^\\d{1,2}[:.]\\d{2}(\\s?[AaPp]\\.?\\s?[Mm]\\.?)?$/;
    const QUOTE = '[data-testid="quoted-message"], [aria-label="Quoted message"], [aria-label^="Quoted"], [class*="quoted"]';
    const inQuote = n => !!(n.closest && n.closest(QUOTE));
    const txt = n => ((n && n.innerText) || '').trim();
    const pTop = panel.getBoundingClientRect().top;
    const y = n => n.getBoundingClientRect().top - pTop + panel.scrollTop;
    const MEDIA = ['audio-play', 'ptt', 'audio-download', 'document', 'video-pip', 'media-play', 'sticker'];
    // A reply's text starts with the message it quotes ("You\\nKARLCON…pdf •
    // 4 pages\\nWe are rather an agent…"). Strip a leading block whose text is
    // exactly the start of the message and shorter than it.
    function stripQuote(el, text) {
        for (const q of el.querySelectorAll(QUOTE + ', div[role="button"], span[role="button"]')) {
            const qt = txt(q);
            if (qt && qt.length < text.length && text.startsWith(qt)) return text.slice(qt.length).trim();
        }
        return text;
    }

    const outermost = Array.from(panel.querySelectorAll('[data-id]'))
        .filter(e => !(e.parentElement && e.parentElement.closest('[data-id]')));
    const units = [];
    const rows = Array.from(panel.querySelectorAll('[role="row"]'))
        .filter(r => !(r.parentElement && r.parentElement.closest('[role="row"]')));
    if (rows.length) {
        for (const r of rows) {
            const m = r.matches('[data-id]') ? r : r.querySelector('[data-id]');
            if (m) units.push({ kind: 'msg', el: m });
            else if (txt(r)) units.push({ kind: 'divider', el: r });
        }
        for (const m of outermost) if (!m.closest('[role="row"]')) units.push({ kind: 'msg', el: m });
    } else {
        for (const m of outermost) units.push({ kind: 'msg', el: m });
    }

    const out = [];
    for (const u of units) {
        const el = u.el;
        if (u.kind === 'divider') { out.push({ type: 'divider', text: txt(el), y: y(el) }); continue; }
        const preEl = Array.from(el.querySelectorAll('[data-pre-plain-text]')).find(n => !inQuote(n));
        const pre = preEl ? (preEl.getAttribute('data-pre-plain-text') || '') : '';
        let text = preEl ? txt(preEl) : '';
        if (!text) {
            const s = Array.from(el.querySelectorAll('span.selectable-text, [data-testid="selectable-text"]'))
                .find(n => !inQuote(n));
            text = txt(s);
        }
        if (!text) {
            const s = Array.from(el.querySelectorAll("span[dir='ltr'], span[dir='auto']"))
                .find(n => !inQuote(n) && !n.hasAttribute('aria-label') && !TIME.test(txt(n)));
            text = txt(s);
        }
        if (text) text = stripQuote(el, text);
        let time = '';
        const meta = el.querySelector('[data-testid="msg-meta"], [data-testid="msg-time"]');
        const cands = meta ? [meta, ...meta.querySelectorAll('span')]
                           : Array.from(el.querySelectorAll('span, div')).filter(n => !inQuote(n));
        for (const n of cands) { const t = txt(n); if (TIME.test(t)) time = t; }
        let img = null;
        for (const c of el.querySelectorAll('img[src]')) {
            const s = c.getAttribute('src') || '';
            if (s.startsWith('data:image/svg') || inQuote(c)) continue;
            const r = c.getBoundingClientRect();
            if (r.width * r.height > 2000) { img = c; break; }
        }
        let media = '';
        for (const i of el.querySelectorAll('[data-icon], [data-testid]')) {
            const k = (i.getAttribute('data-icon') || i.getAttribute('data-testid') || '');
            if (MEDIA.some(m => k.includes(m))) { media = k; break; }
        }
        // WhatsApp keeps a placeholder for messages far from the visible area:
        // the row and its id exist, the content doesn't. Such a row is NOT an
        // empty message — collect_units() scrolls to it to read it.
        const shell = !img && !txt(el);
        const p = probe(el, preEl || img || null);
        out.push({ type: 'msg', dataId: p.dataId, pre, text, time, hasImage: !!img, media, shell,
                   direction: p.direction, via: p.via, y: y(el) });
    }
    out.sort((a, b) => a.y - b.y);   // Array.prototype.sort is stable
    return out;
}"""

SNAPSHOT_SCHEMA = "karlon.chat_snapshot/1"
WA_SELF_SENDER = "You (WhatsApp)"


def walk_chat(page: Page) -> Optional[list[dict]]:
    """Raw rows of the open conversation in screen order (one round-trip)."""
    try:
        return page.evaluate(_WALK_JS)
    except Exception as e:
        print(f"    ! chat walk failed: {e}")
        return None


def _local_tz():
    if WA_UTC_OFFSET_MINUTES is not None:
        return timezone(timedelta(minutes=WA_UTC_OFFSET_MINUTES))
    return datetime.now().astimezone().tzinfo


def _to_utc_iso(local_naive: datetime, tz) -> str:
    return local_naive.replace(tzinfo=tz).astimezone(timezone.utc).isoformat(timespec="microseconds")


def _external_key(wa_id: str, pre: str, key_text: str, kind: str, chat: str, local: str) -> str:
    # These formulas are the persisted identity of every message already in
    # Karlon (they match every earlier bridge build) — changing them would
    # re-import history as new rows.
    if kind == "image" and wa_id:
        return hashlib.sha1(f"img|{wa_id}".encode()).hexdigest()
    if pre:
        return hashlib.sha1(f"{pre}|{key_text}".encode()).hexdigest()
    if wa_id:
        return hashlib.sha1(f"row|{wa_id}|{key_text}".encode()).hexdigest()
    return hashlib.sha1(f"noid|{chat}|{local}|{key_text}".encode()).hexdigest()


def _choose_dayfirst(units: list[dict], today) -> bool:
    """dd/mm or mm/dd for this chat's "[time, date]" metadata. Decided by any
    unambiguous date (a field > 12); if every date is ambiguous (05/09), by
    which reading agrees with the chat's own date dividers, and never one that
    puts messages in the future. Matters beyond display: the snapshot's
    window start is its oldest message, and the server prunes inside that
    window, so a misread month must not stretch it backwards."""
    pres = [u.get("pre", "") for u in units if u.get("type") == "msg" and u.get("pre")]
    decided = detect_dayfirst(pres, default=None)
    if decided is not None or not pres:
        return True if decided is None else decided
    score = {True: 0, False: 0}
    for df in (True, False):
        current = None
        for u in units:
            if u.get("type") == "divider":
                current = parse_divider_date(u.get("text", ""), today, df) or current
                continue
            ts = parse_pre_timestamp(parse_pre_plain(u.get("pre", ""))[0], df)
            if ts is None:
                continue
            if ts.date() > today + timedelta(days=1):
                score[df] -= 5
            elif current is not None:
                score[df] += 1 if ts.date() == current else -1
    return score[False] <= score[True]


def build_chat_snapshot(
    units: list[dict],
    chat_name: str,
    *,
    reached_top: bool = False,
    now_local: Optional[datetime] = None,
    tz=None,
) -> dict:
    """Turn _WALK_JS output into the chat's single JSON snapshot. Pure: no
    browser, no network — which is what lets it be tested exhaustively."""
    tz = tz or _local_tz()
    now_local = now_local or datetime.now(tz).replace(tzinfo=None)
    today = now_local.date()
    dayfirst = _choose_dayfirst(units, today)

    rejected: list[dict] = []
    entries: list[dict] = []
    seen_ids: set[str] = set()
    stats = {"rows": len(units), "unread_dividers": 0, "date_dividers": 0, "duplicates": 0,
             "not_rendered": 0}
    current_date = None
    last_gap = -1          # len(entries) when the last never-rendered row was seen

    def reject(reason: str, text: str) -> None:
        rejected.append({"reason": reason, "text": (text or "")[:120]})

    for u in units:
        if u.get("type") == "divider":
            t = u.get("text", "")
            d = parse_divider_date(t, today, dayfirst)
            if d:
                current_date = d
                stats["date_dividers"] += 1
            elif is_unread_divider(t):
                stats["unread_dividers"] += 1
            else:
                reject("system_notice", t)
            continue

        wa_id = u.get("dataId") or ""
        if wa_id and wa_id in seen_ids:
            stats["duplicates"] += 1
            continue
        if u.get("shell"):
            # WhatsApp never drew this message's content while we scrolled
            # past it. Its content is unknown, so nothing before it may be
            # pruned on the server (see window_start below).
            stats["not_rendered"] += 1
            last_gap = len(entries)
            if wa_id:
                seen_ids.add(wa_id)
            continue
        pre = u.get("pre") or ""
        ts_raw, raw_sender = parse_pre_plain(pre)
        raw_text = (u.get("text") or "").strip()
        if pre and raw_text.startswith(pre):
            raw_text = raw_text[len(pre):].strip()
        key_text = reformat_ad_context_card(raw_text)
        body, trailing_time = split_trailing_time(key_text)
        bubble_time = (u.get("time") or "").strip() or trailing_time
        kind = "image" if u.get("hasImage") else "text"

        if is_system_notice(body):
            reject("system_notice", body)
            continue
        if kind == "text" and not body:
            reject("unsupported_media" if u.get("media") else "empty", u.get("media") or "")
            continue
        if kind == "text" and is_time_only(body):
            reject("time_only", body)
            continue
        if kind == "text" and not pre and not bubble_time:
            # Real message bubbles always carry a time; WhatsApp's own
            # notices ("X added Y", "You're now an admin") never do.
            reject("no_message_metadata", body)
            continue

        local, source = None, None
        if pre:
            local = parse_pre_timestamp(ts_raw, dayfirst)
            if local:
                source = "metadata"
                current_date = local.date()
        if local is None and bubble_time and current_date:
            local = combine(current_date, bubble_time)
            source = "divider+bubble" if local else None

        if wa_id:
            seen_ids.add(wa_id)
        entries.append({
            "wa_id": wa_id, "pre": pre, "raw_sender": raw_sender,
            "direction": u.get("direction") or "unknown", "via": u.get("via") or "",
            "kind": kind, "text": body, "key_text": key_text,
            "_local": local, "_bubble": bubble_time, "time_source": source,
        })

    resolve_directions(entries)

    # Fill missing times from neighbours, forwards then backwards.
    prev = None
    for e in entries:
        if e["_local"] is None and prev is not None:
            guess = combine(prev.date(), e["_bubble"]) if e["_bubble"] else None
            e["_local"] = guess if guess and guess >= prev else prev
            e["time_source"] = "inferred"
        prev = e["_local"] or prev
    nxt = None
    for e in reversed(entries):
        if e["_local"] is None and nxt is not None:
            guess = combine(nxt.date(), e["_bubble"]) if e["_bubble"] else None
            e["_local"] = guess if guess and guess <= nxt else nxt
            e["time_source"] = "inferred"
        nxt = e["_local"] or nxt
    for e in entries:
        if e["_local"] is None:
            e["_local"], e["time_source"] = now_local.replace(second=0, microsecond=0), "inferred"

    # Screen order wins: non-decreasing times, +1 ms per message within a minute.
    clamped = 0
    prev, minute, k = None, None, 0
    for e in entries:
        if prev is not None and e["_local"] < prev:
            e["_local"] = prev
            clamped += 1
        prev = e["_local"]
        m = e["_local"].replace(second=0, microsecond=0)
        k = k + 1 if m == minute else 0
        minute = m
        e["_final"] = e["_local"] + timedelta(milliseconds=k)

    messages = []
    for seq, e in enumerate(entries):
        local_iso = e["_final"].isoformat()
        messages.append({
            "seq": seq,
            "external_key": _external_key(e["wa_id"], e["pre"], e["key_text"], e["kind"], chat_name, local_iso),
            "wa_id": e["wa_id"] or None,
            "direction": e["direction"],
            "sender": WA_SELF_SENDER if e["direction"] == "out" else (e["raw_sender"] or chat_name),
            "kind": e["kind"],
            "text": e["text"],
            "created_at": _to_utc_iso(e["_final"], tz),
            "time_source": e["time_source"],
        })

    by_reason: dict[str, int] = {}
    for r in rejected:
        by_reason[r["reason"]] = by_reason.get(r["reason"], 0) + 1
    by_source: dict[str, int] = {}
    for m in messages:
        by_source[m["time_source"]] = by_source.get(m["time_source"], 0) + 1
    stats.update(messages=len(messages), rejected=by_reason, time_sources=by_source, clamped=clamped)

    scanned_at = _to_utc_iso(now_local, tz)
    # The server prunes imported rows inside [window_start, window_end] that the
    # snapshot doesn't contain. Only a stretch read completely may count: the
    # window starts after the last message whose content was never drawn, and
    # is absent (no pruning) if that was the newest message.
    first_complete = last_gap if last_gap >= 0 else 0
    window_start = messages[first_complete]["created_at"] if first_complete < len(messages) else None
    return {
        "schema": SNAPSHOT_SCHEMA,
        "chat": {
            "id": chat_slug(chat_name),
            "name": chat_name,
            "wa_name": chat_name,
            "avatar_emoji": avatar_tag(chat_name),
        },
        "scanned_at": scanned_at,
        "window_start": window_start,
        "window_end": scanned_at,
        "reached_top": reached_top,
        "complete": stats["not_rendered"] == 0,
        "messages": messages,
        "rejected": rejected,
        "stats": stats,
    }


def _load_history(page: Page, max_scroll_rounds: int) -> bool:
    """Scroll the open chat up until the oldest message stops changing (True:
    reached the top of what WhatsApp will load) or the round cap is hit."""
    try:
        panel = page.evaluate_handle("() => { " + _PANEL_FN + " return findPanel(); }").as_element()
    except Exception:
        panel = None
    if not panel:
        return False
    prev_top, stall = None, 0
    for _ in range(max_scroll_rounds):
        try:
            panel.evaluate("el => { el.scrollTop = 0; }")
        except Exception:
            return False
        time.sleep(0.5)
        try:
            top_id = panel.evaluate(
                "el => { const m = el.querySelector('[data-id]'); return m ? m.getAttribute('data-id') : null; }")
        except Exception:
            top_id = None
        if top_id is not None and top_id == prev_top:
            stall += 1
            if stall >= 2:
                return True
        else:
            stall = 0
        prev_top = top_id
    return False


# How the chat is read. WhatsApp only draws message content near the visible
# part of the list; everything else is an empty placeholder with the right id.
# Reading the whole list from one scroll position (as earlier builds did, from
# the TOP, after loading history) saw the newest messages as blanks: "rejected:
# empty 67", the newest replies missing for hours, and the server pruning then
# re-adding real messages every pass. So the list is read in overlapping
# steps, each step's drawn messages are merged by id, and a message counts
# only once some step has actually drawn it.
STEP_WAIT_SEC = 0.35       # time for WhatsApp to draw a step
STEP_FRACTION = 0.7        # each step moves 70% of a screen (30% overlap)
RECENT_SCREENS = 3         # "recent" mode reads the last ~4 screens
MAX_SCAN_STEPS = 400


def _panel_handle(page: Page):
    try:
        return page.evaluate_handle("() => { " + _PANEL_FN + " return findPanel(); }").as_element()
    except Exception:
        return None


def _panel_state(panel) -> dict:
    return panel.evaluate("el => ({ top: el.scrollTop, h: el.clientHeight, H: el.scrollHeight })")


def _unit_key(u: dict, prev_msg_key: Optional[str]) -> Optional[str]:
    if u.get("type") == "msg":
        if u.get("dataId"):
            return "m:" + u["dataId"]
        return "n:" + (u.get("pre") or "") + "|" + (u.get("text") or "")
    t = (u.get("text") or "").strip()
    return ("d:" + t + "|" + str(prev_msg_key)) if t else None


def _unit_score(u: dict) -> float:
    if u.get("type") != "msg":
        return 1.0
    return ((0 if u.get("shell") else 4) + (2 if u.get("hasImage") else 0)
            + (1 if u.get("pre") else 0) + min(len(u.get("text") or ""), 5000) / 5000)


def merge_walks(walks: list[list[dict]]) -> list[dict]:
    """Merge the rows seen at successive scroll positions into one ordered
    list: rows are identified by WhatsApp message id (dividers by text and
    position), ordered as the walks saw them, and each keeps its best-drawn
    version (a real bubble beats a placeholder)."""
    order: list[str] = []
    best: dict[str, dict] = {}
    for units in walks:
        anchor, prev_msg = None, None
        for u in units:
            key = _unit_key(u, prev_msg)
            if u.get("type") == "msg":
                prev_msg = key
            if key is None:
                continue
            if key in best:
                if _unit_score(u) > _unit_score(best[key]):
                    best[key] = u
                anchor = key
                continue
            best[key] = u
            if anchor is None:
                if order:
                    order.append(key)   # scanning downwards: unanchored = below
                else:
                    order.append(key)
            else:
                order.insert(order.index(anchor) + 1, key)
            anchor = key
    return [best[k] for k in order]


def collect_units(page: Page, mode: str = "full", max_scroll_rounds: int = 12):
    """(units, reached_top) for the open chat, read in overlapping steps.
      mode "full":   load history to the top, then step down to the bottom.
      mode "recent": only the last ~RECENT_SCREENS+1 screens — the fast path
                     for a chat that just got new messages."""
    panel = _panel_handle(page)
    if not panel:
        return None, False
    reached_top = False
    try:
        if mode == "full":
            reached_top = _load_history(page, max_scroll_rounds)
            start = 0
        else:
            panel.evaluate("el => { el.scrollTop = el.scrollHeight; }")
            time.sleep(STEP_WAIT_SEC)
            st = _panel_state(panel)
            start = max(0, st["H"] - (RECENT_SCREENS + 1) * st["h"])
        walks, pos = [], start
        for _ in range(MAX_SCAN_STEPS):
            panel.evaluate(f"el => {{ el.scrollTop = {int(pos)}; }}")
            time.sleep(STEP_WAIT_SEC)
            w = walk_chat(page)
            if w is None:
                break
            walks.append(w)
            st = _panel_state(panel)
            if st["top"] + st["h"] >= st["H"] - 4:
                break
            pos = st["top"] + max(50, int(st["h"] * STEP_FRACTION))
    except Exception as e:
        print(f"    ! scroll-read failed: {e}")
        return None, reached_top
    if not walks:
        return None, reached_top
    return merge_walks(walks), reached_top


def scan_chat(page: Page, chat_name: str, max_scroll_rounds: int = 12,
              mode: str = "full") -> Optional[dict]:
    """Read the open chat (see collect_units) and return its snapshot."""
    units, reached_top = collect_units(page, mode, max_scroll_rounds)
    if units is None:
        _report_dom_once(page, f"no message list found in the open chat '{chat_name}'")
        return None
    snap = build_chat_snapshot(units, chat_name, reached_top=reached_top)
    snap["stats"]["mode"] = mode
    return snap


def save_snapshot(snapshot: dict) -> Path:
    """Write the chat's JSON to CHAT_SNAPSHOT_DIR/<chat id>.json (atomically,
    so a crash never leaves half a file). This is the audit copy: exactly what
    was read from WhatsApp and what was rejected, per chat."""
    out_dir = Path(CHAT_SNAPSHOT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{snapshot['chat']['id']}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def push_snapshot(snapshot: dict) -> Optional[dict]:
    """POST the snapshot to /api/chats/{id}/sync, where the server applies it
    in one transaction. Returns the server's report, or None if this server
    predates the endpoint (caller falls back to the legacy per-message path)."""
    body = {k: v for k, v in snapshot.items() if k != "rejected"}
    r = get_session().post(
        f"{KARLON_URL}/api/chats/{snapshot['chat']['id']}/sync",
        json=body, timeout=UPLOAD_TIMEOUT,
    )
    if r.status_code in (404, 405):
        return None
    r.raise_for_status()
    return r.json()


_FIND_MSG_JS = """(id) => Array.from(document.querySelectorAll('[data-id]'))
    .find(e => e.getAttribute('data-id') === id) || null"""


def _image_b64_for(page: Page, wa_id: str) -> Optional[str]:
    """Bytes of the photo in message `wa_id` of the open chat, as base64.
    The message is scrolled into view first and given time to draw: a photo
    far from the visible area is only a placeholder ("couldn't read photo")."""
    def find():
        try:
            return page.evaluate_handle(_FIND_MSG_JS, wa_id).as_element()
        except Exception:
            return None

    row = find()
    if row is None:                    # not in the DOM at all: search upwards
        panel = _panel_handle(page)
        if panel:
            try:
                panel.evaluate("el => { el.scrollTop = el.scrollHeight; }")
                for _ in range(MAX_SCAN_STEPS):
                    time.sleep(STEP_WAIT_SEC)
                    row = find()
                    st = _panel_state(panel)
                    if row is not None or st["top"] <= 0:
                        break
                    panel.evaluate(f"el => {{ el.scrollTop = {max(0, int(st['top'] - st['h'] * STEP_FRACTION))}; }}")
            except Exception:
                row = None
    if row is None:
        return None
    try:
        row.scroll_into_view_if_needed(timeout=3_000)
    except Exception:
        pass
    deadline = time.time() + 4
    while time.time() < deadline:
        best, best_blob = None, None
        for img in row.query_selector_all("img[src]"):
            src = _safe_get_attr(img, "src")
            if not src or src.startswith("data:image/svg"):
                continue
            box = img.bounding_box()
            if not box or box["width"] * box["height"] <= 2_000:
                continue
            if src.startswith("blob:"):
                best_blob = (img, src)
                break
            best = best or (img, src)
        pick = best_blob or (best if time.time() > deadline - 1.5 else None)
        if pick:
            img, src = pick
            b64 = fetch_blob_b64(page, src)
            if b64:
                return b64
            try:
                return base64.b64encode(img.screenshot()).decode("ascii")
            except Exception:
                return None
        time.sleep(0.3)
    return None


def upload_snapshot_images(page: Page, snapshot: dict, keys: set) -> int:
    """Upload the photos whose external_key is in `keys`, each with its
    WhatsApp time so it lands in the right place in the thread."""
    sent = 0
    chat_id = snapshot["chat"]["id"]
    for m in snapshot["messages"]:
        if m["kind"] != "image" or m["external_key"] not in keys or not m.get("wa_id"):
            continue
        b64 = _image_b64_for(page, m["wa_id"])
        if not b64:
            print(f"    ! couldn't read photo {m['wa_id']}")
            continue
        fd, tmp = tempfile.mkstemp(prefix="karlon_img_", suffix=".jpg")
        with os.fdopen(fd, "wb") as f:
            f.write(base64.b64decode(b64))
        try:
            if import_image_message(chat_id, m["sender"], m["text"], Path(tmp),
                                    external_key=m["external_key"], direction=m["direction"],
                                    created_at=m["created_at"]):
                _imported_image_keys.add(m["external_key"])
                sent += 1
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return sent


def _push_snapshot_legacy(page: Page, snapshot: dict) -> dict:
    """For a server without /sync: same cleaned, ordered, UTC-timed data via
    the old endpoints (no pruning of stale rows — that needs /sync)."""
    chat = snapshot["chat"]
    ensure_chat(chat["name"])
    texts = [
        {k: m[k] for k in ("sender", "text", "created_at", "external_key", "direction")}
        for m in snapshot["messages"] if m["kind"] == "text"
    ]
    result = import_messages(chat["id"], texts)
    keys = {m["external_key"] for m in snapshot["messages"]
            if m["kind"] == "image" and m["external_key"] not in _imported_image_keys}
    result["images_uploaded"] = upload_snapshot_images(page, snapshot, keys)
    result["legacy"] = True
    return result


def import_image_message(
    chat_id: str, sender: str, caption: str, image_path: Path,
    external_key: str, direction: str = "in", created_at: Optional[str] = None,
) -> Optional[dict]:
    """
    Posts to /images/import (the wa_bridge-scraped path, dedup'd by
    external_key, stored with wa_status=NULL so the outbox never re-sends it),
    NOT /images (the app/staff-composed path, which queues a WhatsApp send).

    `created_at` is the photo's WhatsApp time. Earlier builds never sent it,
    so every scraped photo was stamped with the moment it was uploaded and
    appeared at the bottom of the chat, after messages sent hours later.
    """
    data = {
        "sender": sender,
        "caption": caption,
        "external_key": external_key,
        "direction": direction,
    }
    if created_at:
        data["created_at"] = created_at
    try:
        with open(image_path, "rb") as f:
            r = get_session().post(
                f"{KARLON_URL}/api/chats/{chat_id}/images/import",
                data=data,
                files={"file": (image_path.name, f, "image/jpeg")},
                timeout=UPLOAD_TIMEOUT,
            )
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        print(f"    ! image upload to Karlon failed: {e}")
        return None


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
# Chats read in full at least once by this process; after that, a change only
# needs the recent screens re-read (seconds, not minutes, per chat).
_synced_once: set[str] = set()
# Chats that failed to open/read: name -> (failures, retry-after time). A chat
# that can't be opened used to cost ~30 s on EVERY pass, delaying all others.
_chat_failures: dict[str, tuple[int, float]] = {}
FAIL_BACKOFF_SEC = 60
FAIL_BACKOFF_MAX_SEC = 1800


def select_targets(page: Page, sync_all: bool, only_chat: Optional[str], limit: int) -> list[dict]:
    """Which chats this pass reads.
      --chat NAME   just that chat (to check one chat's JSON before a full run)
      default       the `limit` most recent chats in WhatsApp's order
                    (WA_SYNC_LAST_N_CHATS, default 15)
      --all / 0     every chat in the sidebar, filtered by should_sync()"""
    if only_chat:
        name = clean_chat_name(only_chat) or only_chat.strip()
        return [{"name": name, "preview": "", "unread": 0, "pic_src": None, "fingerprint": None}]
    if limit > 0 and not sync_all:
        return get_recent_chat_rows(page, limit)
    return [c for c in get_all_chat_rows(page) if should_sync(c, sync_all)]


def sync_chat(page: Page, name: str, fetch_pics: bool, max_scroll_rounds: int,
              dry_run: bool = False, mode: str = "full") -> Optional[dict]:
    """Open one chat, build its snapshot, save it, and (unless dry_run) apply
    it on the server. Returns the snapshot, or None if the chat was skipped."""
    if not open_chat_row(page, name):
        print(f"  ! could not open: {name}")
        return None
    # Identity gate, same rule as outbound sends: the conversation WhatsApp
    # actually has open must be the one we meant, or nothing is written.
    if header_shows(page, name) is False:
        print(f"  ! opened '{_short(get_open_chat_title(page))}' while looking for '{name}' — skipping this chat")
        return None

    t0 = time.time()
    snapshot = scan_chat(page, name, max_scroll_rounds=max_scroll_rounds, mode=mode)
    if snapshot is None:
        print(f"  ! couldn't read messages for {name}")
        return None
    path = save_snapshot(snapshot)
    st = snapshot["stats"]
    rej = ", ".join(f"{k} {v}" for k, v in sorted(st["rejected"].items())) or "none"
    if st.get("not_rendered"):
        rej += f"; {st['not_rendered']} not drawn (no pruning before them)"
    label = "\U0001f4f1 " if is_unsaved_number(name) else "   "
    if dry_run:
        print(f"  {label}{name}: {st['messages']} message(s), rejected: {rej}  → {path}")
        return snapshot

    for m in snapshot["messages"]:
        if m["direction"] == "in" and m["text"]:
            _record_inbound_text(m["text"])

    if fetch_pics:
        b64 = get_profile_photo_b64(page, debug=False)
        if b64:
            try:
                ensure_chat(name, pic_url=save_profile_pic(chat_slug(name), b64))
            except Exception as e:
                print(f"    ! pic save failed for {name}: {e}")

    try:
        report = push_snapshot(snapshot)
    except requests.HTTPError as e:
        resp = e.response
        print(f"  ! server refused {name}'s snapshot "
              f"({resp.status_code if resp is not None else '?'}: "
              f"{(resp.text if resp is not None else str(e))[:200]}) — kept at {path}")
        return None
    if report is None:
        report = _push_snapshot_legacy(page, snapshot)
        print(f"  {label}{name}: +{report['imported']} new (skipped {report['skipped']}), "
              f"rejected: {rej}  [server has no /sync — deploy server/ to enable cleanup]")
        return snapshot
    missing = set(report.get("missing_images") or [])
    uploaded = upload_snapshot_images(page, snapshot, missing) if missing else 0
    skipped_note = f"  (prune skipped: {report['prune_skipped']})" if report.get("prune_skipped") else ""
    print(
        f"  {label}{name} [{mode}, {time.time() - t0:.1f}s]: {st['messages']} msg — +{report.get('inserted', 0)} new, "
        f"{report.get('updated', 0)} fixed, {report.get('pruned', 0)} stale removed"
        f"{f', {uploaded} photo(s)' if uploaded else ''}; rejected: {rej}{skipped_note}"
    )
    return snapshot


def sync_once(page: Page, sync_all: bool, fetch_pics: bool, max_scroll_rounds: int = 12,
              only_chat: Optional[str] = None, limit: Optional[int] = None,
              dry_run: bool = False) -> None:
    global _sync_pass
    _sync_pass += 1
    full_sweep = only_chat is not None or (_sync_pass - 1) % WA_FULL_SWEEP_EVERY == 0
    limit = WA_SYNC_LAST_N_CHATS if limit is None else limit
    targets = select_targets(page, sync_all, only_chat, limit)
    scope = (f"chat {only_chat!r}" if only_chat
             else f"last {limit} chats" if limit > 0 and not sync_all else "all flagged chats")
    print(f"Syncing {len(targets)} chat(s) ({scope}){'  [full sweep]' if full_sweep else ''}"
          f"{'  [dry run — JSON only]' if dry_run else ''}")
    skipped = backing_off = 0
    for c in targets:
        # Outbound (messages / invoices) always jumps the queue ahead of
        # continuing to walk the chat list — check before every chat.
        if outbox_pending() and not dry_run:
            process_outbox(page)
        name = c["name"]
        fp = c.get("fingerprint")
        if not full_sweep and fp and _chat_fingerprints.get(name) == fp:
            skipped += 1
            continue
        fail = _chat_failures.get(name)
        if fail and time.time() < fail[1] and not only_chat:
            backing_off += 1
            continue
        mode = "full" if (only_chat or name not in _synced_once) else "recent"
        try:
            snap = sync_chat(page, name, fetch_pics, max_scroll_rounds, dry_run=dry_run, mode=mode)
        except requests.RequestException as e:
            print(f"  ! Karlon unreachable after retries ({e}) — check {KARLON_URL}, "
                  f"skipping {name!r} this pass")
            continue
        if snap is None:
            n = (fail[0] + 1) if fail else 1
            wait = min(FAIL_BACKOFF_MAX_SEC, FAIL_BACKOFF_SEC * 2 ** (n - 1))
            _chat_failures[name] = (n, time.time() + wait)
            print(f"    (will retry {name!r} in {wait // 60 or 1} min)")
            continue
        _chat_failures.pop(name, None)
        _synced_once.add(name)
        if fp:
            _chat_fingerprints[name] = fp
    if skipped:
        print(f"  ({skipped} chat(s) unchanged since last pass — not re-read)")
    if backing_off:
        print(f"  ({backing_off} chat(s) waiting to retry after failing to open)")


# ─────────────────────────────── entry point ──────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once",     action="store_true", help="sync one pass then exit")
    ap.add_argument("--all",      action="store_true",
                    help="sync every chat in the sidebar instead of only the most recent ones")
    ap.add_argument("--chat",     metavar="NAME", help="sync just this one chat (e.g. --chat Karl)")
    ap.add_argument("--limit",    type=int, default=None,
                    help=f"how many of the most recent chats to sync (default {WA_SYNC_LAST_N_CHATS}; 0 = all flagged)")
    ap.add_argument("--dry-run",  action="store_true",
                    help="read chats and write their JSON snapshots, but send nothing to Karlon")
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
        if not args.dry_run:
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
    if not args.dry_run:
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
    if not args.dry_run:
        urgent_poller.start()

    try:
        if args.discover:
            dump_selectors(page)
            print("\nRow selector scan:")
            discover_row_selector(page)
            return

        last_reload = time.time()
        while True:
            if WA_RELOAD_HOURS and time.time() - last_reload > WA_RELOAD_HOURS * 3600:
                reload_whatsapp(page)
                last_reload = time.time()
            # 1. Import new messages from WhatsApp → Karlon
            sync_once(
                page,
                sync_all=args.all,
                fetch_pics=args.pics,
                max_scroll_rounds=60 if args.deep_history else 12,
                only_chat=args.chat,
                limit=args.limit,
                dry_run=args.dry_run,
            )

            # 2. Process outbound queue: send Karlon messages → WhatsApp via DOM
            if not args.dry_run:
                process_outbox(page)

            if args.once or args.chat or args.dry_run:
                break
            print(f"Idle {POLL_INTERVAL}s (delivering outbound as it arrives)...  "
                  f"(outbox thread polls every {OUTBOX_POLL_SEC}s)\n")
            idle_wait(page, POLL_INTERVAL)

    finally:
        stop_event.set()
        for t in (poller, urgent_poller):
            if t.is_alive():
                t.join(timeout=3)
        ctx.close()
        pw.stop()


if __name__ == "__main__":
    main()