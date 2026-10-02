"""config.py — central paths and settings shared across the app.

RECONSTRUCTED: the original file failed to extract from the .rar archive
(0 bytes). Rebuilt from every import site that references it:
  - main.py:             ALLOWED_ORIGINS, STATIC_DIR
  - media.py:             MEDIA_KIND_BY_EXT
  - routers/messages.py:  MEDIA_DIR, MEDIA_KIND_BY_EXT
Please review before relying on this in production — in particular
ALLOWED_ORIGINS below defaults to "*" (open) to match main.py's comment
that "a phone on another network can call the API"; tighten this if you
want to restrict which origins may call the server.
"""

from pathlib import Path

# server/app/config.py -> parents[1] == server/
BASE_DIR = Path(__file__).resolve().parents[1]

STATIC_DIR = BASE_DIR / "static"
MEDIA_DIR = STATIC_DIR / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

# Generated invoice PDFs — served the same way as /static/media, kept in
# their own folder so they're easy to find/back up separately.
INVOICES_DIR = STATIC_DIR / "invoices"
INVOICES_DIR.mkdir(parents=True, exist_ok=True)

# Static (non-generated) documents the app can push to a guest on demand —
# e.g. "Send T&Cs". Unlike invoices these aren't built per-booking; the same
# file is reused for every send, so it just needs to exist once here.
DOCUMENTS_DIR = STATIC_DIR / "documents"
DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
TERMS_PDF_PATH = DOCUMENTS_DIR / "terms_and_conditions.pdf"

# Server-hosted copies of scraped listing photos, one folder per listing
# (see routers/houses.py: _cache_listing_images). Served at /static/houses/...
HOUSES_MEDIA_DIR = STATIC_DIR / "houses"
HOUSES_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
# WhatsApp profile pictures uploaded by wa_bridge.py, one file per chat
# (routers/chats.py: upload_profile_pic). Served at /static/profile_pics/...
PROFILE_PICS_DIR = STATIC_DIR / "profile_pics"
PROFILE_PICS_DIR.mkdir(parents=True, exist_ok=True)
PROFILE_PIC_MAX_BYTES = 2 * 1024 * 1024
# How many of a listing's photos get mirrored locally + sent over WhatsApp.
HOUSE_IMAGES_TO_CACHE = 6
HOUSE_IMAGE_FETCH_TIMEOUT_SEC = 20

# Default booking window for a city search that arrives WITHOUT dates. Must
# match airbnb_parallel_system.py's CHECKIN_DAYS_FROM_NOW / STAY_NIGHTS so an
# undated app search and the scraper's full sweep price the same window.
DEFAULT_CHECKIN_DAYS_FROM_NOW = 14
DEFAULT_STAY_NIGHTS = 2

# A refresh job that has sat 'in_progress' longer than this is assumed to
# have died with the scraper process and is handed out again on the next
# /refresh/pending poll.
REFRESH_JOB_STALE_MINUTES = 30

DB_PATH = BASE_DIR / "karlon.db"

# Timestamps are STORED in UTC (so they sort) and SERVED at this offset, in
# minutes east of UTC (Harare = +120). The Android app displays the ISO digits
# it receives, so serving UTC showed every message two hours early.
import os as _os
DISPLAY_TZ_OFFSET_MINUTES = int(_os.environ.get("DISPLAY_TZ_OFFSET_MINUTES", "120"))

# ── invoice property catalogue ──────────────────────────────────────────────
# Keyed by location (shown as the first dropdown in the app's Invoice tab).
# Each entry is a list of {name, rate} for the second dropdown. PLACEHOLDER
# DATA — only fill in locations/properties/rates you actually operate;
# delete or edit any of this before relying on it for real invoices.
PROPERTY_CATALOGUE: dict[str, list[dict]] = {
    "Bulawayo": [
        {"name": "The Residence41", "rate": 120.0},
    ],
    "Harare": [
        # {"name": "<real listing name>", "rate": 0.0},
    ],
}

# Open by default so any device on any network (LAN, tunnel, or a phone
# talking to the HF Space) can call the API. Tighten if needed.
ALLOWED_ORIGINS = ["*"]

MEDIA_KIND_BY_EXT = {
    "image": {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic"},
    "audio": {".mp3", ".wav", ".m4a", ".ogg", ".opus", ".aac"},
    "video": {".mp4", ".mov", ".webm", ".mkv", ".avi"},
    "document": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".csv"},
}
