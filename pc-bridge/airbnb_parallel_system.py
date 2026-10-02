#!/usr/bin/env python3
"""
Airbnb Parallel Automation System – Fixed
All timeouts, modals, and async errors resolved.
"""

import sys

# Windows PowerShell's console defaults to the cp1252 codepage, which can't
# encode emoji (e.g. the 🔎 in prompt_for_search_queries()). Without this,
# the script crashes with UnicodeEncodeError on its very first print() every
# time it's launched under the supervisor, causing an endless crash-loop
# restart. Force UTF-8 on stdout/stderr before any print() runs.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        # reconfigure() needs Python 3.7+; extremely old interpreters just
        # keep the default codepage and risk the original crash.
        pass

import asyncio
import json
import csv
import os
import re
import time
import random
from itertools import cycle
from pathlib import Path
from typing import List, Dict, Set, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, date

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

# ---------- CONFIGURATION ----------
# Every Zimbabwean city we search. Add/remove freely — the search worker below
# processes this list ONE CITY AT A TIME and fully exhausts each city's results
# (every page of the search results, not just the first screen) before moving
# on to the next city. See MAX_SEARCH_WORKERS and perform_search()'s pagination
# loop for how that's enforced.
# Trimmed to Harare only per request. The rest of the towns are kept here, commented
# out, so it's a one-line uncomment to bring the full sweep back later.
ZIMBABWE_CITIES = [
    "Harare",
    # "Bulawayo",
    # "Masvingo",
    # "Gweru",
    # "Mutare",
    # "Chinhoyi",
    # "Kwekwe",
    # "Kadoma",
    # "Chegutu",
    # "Marondera",
    # "Victoria Falls",
    # "Bindura",
    # "Beitbridge",
    # "Gwanda",
    # "Zvishavane",
    # "Chiredzi",
    # "Kariba",
    # "Rusape",
    # "Chipinge",
    # "Norton",
]
SEARCH_QUERIES = list(ZIMBABWE_CITIES)
LISTING_URLS = []

# Some of these names aren't unique to Zimbabwe (e.g. "Norton" is also a town in South
# Africa/UK/US; "Beitbridge" sits right on the SA border). perform_search() appends
# ", Zimbabwe" to the text actually typed into Airbnb's search box to steer autocomplete
# correctly, and as a second, harder guarantee, verify_search_is_zimbabwe() checks the
# actual country after the search loads and refuses to scrape a city's results if the
# search landed somewhere else (South Africa etc).
DISALLOWED_COUNTRY_HINTS = ["south africa", "sandton", "johannesburg", "pretoria", "cape town", "durban"]

MAX_LISTINGS_PER_SEARCH = 0   # 0 = no cap — pull EVERY listing found for a city via pagination
MAX_PAGES_PER_CITY = 40       # hard safety ceiling so a buggy "next page" loop can't run forever
MIN_IMAGES_PER_LISTING = 8    # try to get at least this many per listing
MAX_IMAGES_PER_LISTING = 0    # 0 = no cap; download every photo the listing's gallery has

# Distributed priority: several cities are searched concurrently instead of one
# fully-exhausted city at a time. With MAX_SEARCH_WORKERS=1 a slow/heavy city (e.g.
# Bulawayo) blocked every other city behind it in the queue — the whole run "stuck"
# on one city. With N workers, up to N cities are in flight together, each pulling
# listings off the search_queue and feeding listing_queue independently, so a slow
# city no longer starves the rest. MAX_SECONDS_PER_CITY (see perform_search) is the
# per-city safety valve — even a single stuck city can only hold its own worker
# hostage for so long before it's cut loose and the worker grabs the next city.
MAX_SEARCH_WORKERS = 40
MAX_SECONDS_PER_CITY = 900000   # 15 min hard cap per city, on top of MAX_PAGES_PER_CITY
MAX_SCRAPE_WORKERS = 4
MAX_MESSAGE_WORKERS = 2
MAX_IMAGE_DOWNLOADS = 20

# How long to rest between full re-scans of all ZIMBABWE_CITIES once a cycle finishes.
# The script never exits on its own anymore — main() loops forever, sleeping this long
# between cycles, until you Ctrl+C it. checkpoint.scraped_urls / sent_urls keep listings
# from being re-scraped/re-messaged pointlessly on later cycles (see scrape_worker /
# message_worker), so each cycle mainly picks up NEW listings + refreshed prices.
CYCLE_REST_SECONDS = 1   # 30 minutes; raise/lower to taste

# Master switch for the "message every host" phase. Set to False to pause outreach
# while you keep scraping/updating listings, prices, and coordinates — flip it back
# to True whenever you want messaging to resume. checkpoint.sent_urls still tracks
# who's already been messaged, so nothing gets double-messaged when you turn it back on.
ENABLE_MESSAGING = False

# Dates picked in the search calendar (see select_dates()) AND baked into every listing
# URL before it's scraped (see dated_listing_url()) — Airbnb often won't show a real
# price ("Add dates to see prices") without dates in place, so both the search flow and
# the per-listing scrape carry these same dates.
CHECKIN_DAYS_FROM_NOW = 14
STAY_NIGHTS = 2

MIN_DELAY = 800
MAX_DELAY = 2500
NAV_TIMEOUT = 120000          # 2 minutes
# Was a hardcoded 180s (3 min) — a slow/2FA manual login could blow through
# that before login was ever detected, aborting the whole run with nothing
# scraped or saved. Now sourced from local_config.py (default 360s) and
# overridable via AIRBNB_LOGIN_TIMEOUT_SECONDS without editing this file.
from local_config import AIRBNB_LOGIN_TIMEOUT_SECONDS as LOGIN_TIMEOUT_SECONDS

OUTPUT_DIR = "./airbnb_data"
MASTER_INDEX_FILE = Path(OUTPUT_DIR) / "master_index.json"
CSV_FILE = Path(OUTPUT_DIR) / "master_index.csv"
CHECKPOINT_FILE = Path(OUTPUT_DIR) / ".checkpoint.json"
STORAGE_STATE_FILE = Path(OUTPUT_DIR) / "auth_state.json"  # saved login profile (cookies + localStorage)
COORDS_FILE = Path(OUTPUT_DIR) / "coordinates.json"        # {listingId: {lat, lng, location}}

# ---------- CURRENCY CONVERSION (ZAR -> USD) ----------
# Airbnb prices most Zimbabwe listings in ZAR. Instead of a hardcoded guess-rate,
# this pulls a real live rate from a free FX API at startup, caches it on disk for
# FX_CACHE_MAX_AGE_HOURS so you're not hitting the API on every single run, and only
# falls back to a fixed approximate rate if every live source is unreachable.
FX_BASE_CURRENCY = "ZAR"
FX_API_URLS = [
    f"https://open.er-api.com/v6/latest/{FX_BASE_CURRENCY}",
    f"https://api.exchangerate-api.com/v4/latest/{FX_BASE_CURRENCY}",
    f"https://api.frankfurter.app/latest?from={FX_BASE_CURRENCY}&to=USD",
]
FX_CACHE_FILE = Path(OUTPUT_DIR) / ".fx_rate_cache.json"
FX_CACHE_MAX_AGE_HOURS = 12
ZAR_TO_USD_RATE_FALLBACK = 20.0   # only used if every live FX lookup fails

# Set once at startup by fetch_zar_to_usd_rate() in main(), then read everywhere else.
# (A module-level mutable is simplest here since every worker just needs to *read* the
# same number — nothing writes it after main() finishes the startup FX fetch.)
CURRENT_ZAR_TO_USD_RATE = ZAR_TO_USD_RATE_FALLBACK

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# ---------- KARLON SERVER BRIDGE (scraper -> server -> app) ----------
# Every listing this scraper indexes gets POSTed to
# {KARLON_SERVER_URL}/api/houses/ingest (see server/app/routers/houses.py) so
# the Karlon app's house-icon button can pull it via
# GET /api/houses/available?location=..&limit=6 the moment it's tapped — the
# server sits in the middle, scraper -> server -> app, exactly like the
# invoice/terms PDF flow already does for outbound WhatsApp messages.
# Was hardcoded to "http://127.0.0.1:8000" here — meaning this scraper could
# silently push listings to a *different* server than wa_bridge.py/
# invoice_worker.py were talking to. Now reads the same KARLON_URL as
# everything else (override via the KARLON_URL env var, or local_config.py).
from local_config import KARLON_URL as KARLON_SERVER_URL, KARLON_API_TOKEN
ENABLE_KARLON_PUSH = True
# Cold HuggingFace Spaces can take 20-30s to answer their first request after
# idling — 10s made the very first ingest of a cycle fail. 60s covers a wake-up.
KARLON_PUSH_TIMEOUT_SECONDS = 60

# ---- On-demand re-pricing (arbitrary check-in/check-out from the app) ----
# The full sweep above always prices CHECKIN_DAYS_FROM_NOW/STAY_NIGHTS. This lets a
# guest picking a different date pair in the Android app's AvailableHousesDialog
# trigger a real, small, fast re-scrape for exactly those dates, instead of getting
# an empty result because nothing in house_listings matches their exact dates.
# Requires the server to expose GET {prefix}/pending and POST {prefix}/{id}/complete
# (see run_ondemand_job/ondemand_poll_loop below) — if that endpoint doesn't exist
# yet, polling just logs once and keeps retrying; the normal full-sweep cycle is
# completely unaffected either way.
ENABLE_ONDEMAND_REFRESH = True
ONDEMAND_REFRESH_PATH_PREFIX = "/api/houses/refresh"
ONDEMAND_POLL_INTERVAL_SECONDS = 20
ONDEMAND_MAX_LISTINGS_PER_JOB = 8   # small + fast — a guest is waiting on this, unlike the full sweep
# How many of each listing's photos get pushed up — the app only ever shows/
# sends a handful per property, so there's no point pushing every photo.
KARLON_MAX_IMAGES_PER_LISTING = 6

# ---------- STATE & CHECKPOINT ----------
@dataclass
class Checkpoint:
    scraped_urls: Set[str] = field(default_factory=set)
    sent_urls: Set[str] = field(default_factory=set)

    def save(self):
        data = {
            "scraped_urls": list(self.scraped_urls),
            "sent_urls": list(self.sent_urls),
        }
        CHECKPOINT_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def load(cls):
        if CHECKPOINT_FILE.exists():
            data = json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
            return cls(set(data.get("scraped_urls", [])), set(data.get("sent_urls", [])))
        return cls()

# ---------- HELPERS ----------
def sanitize_folder_name(name: str, max_len: int = 80) -> str:
    if not name:
        return "unknown_listing"
    cleaned = re.sub(r"[^\w\s-]", "", name).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned[:max_len] or "unknown_listing"

async def sleep_ms(ms: int) -> None:
    await asyncio.sleep(ms / 1000.0)

async def random_delay(min_ms: int = MIN_DELAY, max_ms: int = MAX_DELAY) -> None:
    await sleep_ms(random.randint(min_ms, max_ms))

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"

# ---------- LIVE CURRENCY CONVERSION ----------
def _load_cached_fx_rate() -> Optional[float]:
    if not FX_CACHE_FILE.exists():
        return None
    try:
        cached = json.loads(FX_CACHE_FILE.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(cached["fetchedAt"].replace("Z", ""))
        age_hours = (datetime.now(timezone.utc).replace(tzinfo=None) - fetched_at).total_seconds() / 3600
        if age_hours <= FX_CACHE_MAX_AGE_HOURS and cached.get("usdRate"):
            return float(cached["usdRate"])
    except Exception:
        pass
    return None


def _save_cached_fx_rate(rate: float, source: str) -> None:
    try:
        Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
        FX_CACHE_FILE.write_text(json.dumps({
            "usdRate": rate, "source": source, "fetchedAt": now_iso(),
        }, indent=2), encoding="utf-8")
    except Exception:
        pass


async def fetch_zar_to_usd_rate() -> float:
    """Fetch a real, live ZAR->USD rate (how many ZAR per 1 USD) so listing prices
    convert to something actually accurate instead of a stale hand-set number.
    Tries a disk cache first (see FX_CACHE_MAX_AGE_HOURS), then each API in
    FX_API_URLS in turn, and only falls back to ZAR_TO_USD_RATE_FALLBACK if every
    live source fails (e.g. no internet on the box running this)."""
    cached = _load_cached_fx_rate()
    if cached:
        print(f"💱 Using cached ZAR→USD rate: 1 USD = R{cached:.4f} (cached, refreshed every {FX_CACHE_MAX_AGE_HOURS}h)")
        return cached

    import aiohttp
    for api_url in FX_API_URLS:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url, timeout=10) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    # All three sources (open.er-api.com, exchangerate-api.com,
                    # frankfurter.app) return the same shape when queried with
                    # base=ZAR: {"rates": {"USD": 0.054, ...}} — i.e. "1 ZAR buys
                    # this many USD". We invert it to "ZAR per 1 USD" so it matches
                    # the meaning of ZAR_TO_USD_RATE_FALLBACK.
                    usd_per_zar = float(data.get("rates", {}).get("USD") or 0)
                    if usd_per_zar > 0:
                        zar_per_usd = 1.0 / usd_per_zar
                        _save_cached_fx_rate(zar_per_usd, api_url)
                        print(f"💱 Live ZAR→USD rate fetched: 1 USD = R{zar_per_usd:.4f} (source: {api_url})")
                        return zar_per_usd
        except Exception as e:
            print(f"  (FX source {api_url} failed: {e} — trying next)")
            continue

    print(f"⚠️  Could not reach any live FX source — falling back to fixed rate "
          f"1 USD = R{ZAR_TO_USD_RATE_FALLBACK:.2f}. Update ZAR_TO_USD_RATE_FALLBACK "
          f"by hand if this drifts far from the real rate.")
    return ZAR_TO_USD_RATE_FALLBACK


_karlon_session = None


async def _karlon_http():
    """One pooled aiohttp session for every Karlon call this process makes.
    push_listing_to_karlon and the on-demand poller each used to open (and
    tear down) a new ClientSession — a fresh TCP+TLS handshake per listing and
    per 20 s poll against a HuggingFace Space."""
    global _karlon_session
    import aiohttp
    if _karlon_session is None or _karlon_session.closed:
        headers = {"Authorization": f"Bearer {KARLON_API_TOKEN}"} if KARLON_API_TOKEN else {}
        _karlon_session = aiohttp.ClientSession(headers=headers)
    return _karlon_session


async def push_listing_to_karlon(
    data: Dict,
    price_detail: Dict,
    checkin_date: Optional[date] = None,
    checkout_date: Optional[date] = None,
    refresh_job_id: Optional[int] = None,
) -> None:
    """Best-effort POST of one freshly-scraped listing to the Karlon server's
    /api/houses/ingest endpoint so the app's house-icon popup can show it as
    'available now'. Never raises — a wrong/unreachable KARLON_SERVER_URL
    must never interrupt the scrape itself, it just prints a warning and
    moves on."""
    if not ENABLE_KARLON_PUSH:
        return

    # check_in/check_out this specific listing was actually priced for (see
    # dated_listing_url/select_dates) — sent along so the app's popup can filter
    # "only show me listings priced for these exact dates" instead of just
    # whatever was scraped most recently. Callers running the full fixed-window
    # sweep pass nothing and get the old CHECKIN_DAYS_FROM_NOW/STAY_NIGHTS
    # behaviour; run_ondemand_job() passes the guest's actual picked dates.
    if checkin_date is None or checkout_date is None:
        checkin_date = datetime.now(timezone.utc).date() + timedelta(days=CHECKIN_DAYS_FROM_NOW)
        checkout_date = checkin_date + timedelta(days=STAY_NIGHTS)

    nights = (checkout_date - checkin_date).days if (checkin_date and checkout_date) else price_detail.get("nights")
    payload = {
        "listings": [{
            "url": data["url"],
            "listing_id": str(data.get("listingId") or "") or None,
            "title": data.get("title") or "",
            "location": data.get("searchCity") or data.get("location") or "",
            "check_in": checkin_date.isoformat(),
            "check_out": checkout_date.isoformat(),
            "nights": nights,
            # Full ZAR->USD breakdown so the server can store both figures and
            # the app/invoice can show USD while keeping the ZAR original + the
            # exact rate it was divided by (audit: "store both values").
            "price_raw": price_detail.get("raw"),
            "price_currency": price_detail.get("currency"),
            "price_zar_per_night": price_detail.get("per_night") if price_detail.get("currency") == "ZAR" else None,
            "price_usd_per_night": price_detail.get("usd_per_night"),
            "fx_rate_zar_per_usd": price_detail.get("fxRateUsed"),
            "images": (data.get("images") or [])[:KARLON_MAX_IMAGES_PER_LISTING],
            "neighbourhood": data.get("neighbourhood") or None,
            "capacity": data.get("capacity") or None,
            "rating": str(data.get("rating") or "") or None,
            "reviews_count": str(data.get("reviewsCount") or "") or None,
            "lat": data.get("lat"),
            "lng": data.get("lng"),
            # Set on on-demand jobs so the server can count live progress and
            # attribute the offer to the guest's refresh request.
            "refresh_job_id": refresh_job_id,
        }]
    }
    try:
        import aiohttp
        session = await _karlon_http()
        async with session.post(
            f"{KARLON_SERVER_URL}/api/houses/ingest",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=KARLON_PUSH_TIMEOUT_SECONDS),
        ) as resp:
            if resp.status != 200:
                print(f"  ⚠️  Karlon ingest returned {resp.status} for {data['url']}")
    except Exception as e:
        print(f"  ⚠️  Karlon push failed for {data['url']}: {e} "
              f"(check KARLON_SERVER_URL={KARLON_SERVER_URL})")


# ---------- PRICE PARSING (raw scraped text -> structured, converted numbers) ----------
ZAR_AMOUNT_RE = re.compile(r"(?<![A-Za-z])R\s?([\d,]+(?:\.\d+)?)(?:\s?ZAR)?(?![A-Za-z])")
USD_AMOUNT_RE = re.compile(r"\$([\d,]+(?:\.\d+)?)")
NIGHTS_RE = re.compile(r"for\s+(\d+)\s+nights?", re.IGNORECASE)


PER_NIGHT_RE = re.compile(r"(/\s*night|per\s+night|\bnight\b(?!s))", re.IGNORECASE)
RANGE_RE = re.compile(r"\d\s*[-–]\s*(?:R|\$)?\s?\d")


def parse_price_text(raw: str, expected_nights: Optional[int] = None) -> Dict:
    """Turn a raw price-widget string into structured numbers: the stay total
    in the quoted currency, the nights it covers, the per-night amount, and USD
    equivalents (via the live CURRENT_ZAR_TO_USD_RATE, or straight through if
    the listing was already priced in $).

    Per night = total / nights. With dates in the URL, Airbnb's headline price
    is the WHOLE-STAY total ("$3,172" with "for 22 nights" beside it). Earlier
    builds often captured only "$3,172", found no night count, and stored the
    stay total as the nightly rate. Now, in order:
      * "… for N nights"      -> total / N
      * "$144 night" / "/night" -> already per night
      * bare amount            -> total for `expected_nights` (the stay length
                                  this scrape asked Airbnb to price)
    A struck-through original next to a discounted price ("$3,500 $3,172")
    takes the LAST (actual) amount; a genuine range ("R1,200 - R1,500") is
    averaged as before."""
    empty = {
        "raw": raw, "currency": None, "amount": None, "nights": None,
        "per_night": None, "usd_total": None, "usd_per_night": None,
        "fxRateUsed": None,
    }
    if not raw:
        return empty

    def to_float(s: str) -> float:
        return float(s.replace(",", ""))

    all_zar = [to_float(x) for x in ZAR_AMOUNT_RE.findall(raw)]
    all_usd = [to_float(x) for x in USD_AMOUNT_RE.findall(raw)]
    if all_zar:
        currency, amounts = "ZAR", all_zar
    elif all_usd:
        currency, amounts = "USD", all_usd
    else:
        return empty
    amount = (sum(amounts) / len(amounts)) if (len(amounts) > 1 and RANGE_RE.search(raw)) else amounts[-1]

    nights_m = NIGHTS_RE.search(raw)
    if nights_m and int(nights_m.group(1)) > 0:
        nights = int(nights_m.group(1))
        per_night, total = amount / nights, amount
    elif PER_NIGHT_RE.search(raw):
        nights = expected_nights
        per_night = amount
        total = amount * nights if nights else amount
    elif expected_nights and expected_nights > 0:
        nights = expected_nights
        per_night, total = amount / nights, amount
    else:
        nights, per_night, total = None, amount, amount

    rate = CURRENT_ZAR_TO_USD_RATE if currency == "ZAR" else 1.0
    return {
        "raw": raw,
        "currency": currency,
        "amount": round(total, 2),
        "nights": nights,
        "per_night": round(per_night, 2),
        "usd_total": round(total / rate, 2),
        "usd_per_night": round(per_night / rate, 2),
        "fxRateUsed": CURRENT_ZAR_TO_USD_RATE if currency == "ZAR" else None,
    }


def format_price_for_index(parsed: Dict) -> str:
    """Human-readable summary stored in master_index/info.json's 'price' field."""
    if not parsed.get("raw"):
        return ""
    if parsed["usd_per_night"] is None:
        return parsed["raw"]
    nightly = f"${parsed['usd_per_night']:.0f}/night"
    if parsed["currency"] == "ZAR":
        return f"{parsed['raw']} (~{nightly} USD)"
    return parsed["raw"]


def dated_listing_url(base_url: str, days_from_now: int = CHECKIN_DAYS_FROM_NOW, nights: int = STAY_NIGHTS) -> str:
    """Bake check_in/check_out into a listing URL so it loads with a real price
    already showing, instead of Airbnb's undated 'Add dates to see prices' state."""
    checkin = datetime.now(timezone.utc).date() + timedelta(days=days_from_now)
    checkout = checkin + timedelta(days=nights)
    base = base_url.split("?")[0]
    return (
        f"{base}?adults=1&guests=1"
        f"&check_in={checkin:%Y-%m-%d}&check_out={checkout:%Y-%m-%d}"
    )


async def fill_dates_via_widget(page: Page, days_from_now: int = CHECKIN_DAYS_FROM_NOW, nights: int = STAY_NIGHTS) -> bool:
    """Fallback for listings that ignore the check_in/check_out URL params and keep
    showing 'Add dates for prices' — opens the booking-widget calendar on the listing
    page itself and clicks check-in/check-out day cells, same data-testid pattern as
    select_dates() uses on the search page (same underlying date-picker component)."""
    checkin = datetime.now(timezone.utc).date() + timedelta(days=days_from_now)
    checkout = checkin + timedelta(days=nights)

    checkin_field_selectors = [
        '[data-testid="change-dates-checkIn"]',
        'button:has-text("Add dates for prices")',
        'text=/Add dates for prices/i',
        'div:has-text("CHECK-IN")',
    ]
    opened = False
    for sel in checkin_field_selectors:
        field = page.locator(sel).first
        if await field.count() > 0:
            opened = await robust_click(field, page, timeout=4000, attempts=2, clear_overlays=False)
            if opened:
                break
    if not opened:
        return False

    await random_delay()

    async def click_day(d) -> bool:
        testid = f'calendar-day-{d.strftime("%d/%m/%Y")}'
        cell = page.locator(f'[data-testid="{testid}"]').first
        if await cell.count() == 0:
            return False
        return await robust_click(cell, page, attempts=2, clear_overlays=False)

    ok_in = await click_day(checkin)
    await random_delay()
    ok_out = await click_day(checkout) if ok_in else False
    await random_delay()
    return ok_in and ok_out


# ---------- COORDINATES ----------
async def scrape_coordinates(page: Page):
    """Airbnb embeds each listing's map pin as Open Graph place:location meta tags
    (used for social-share map previews). Falls back to a raw-HTML regex scan for
    the same lat/lng pair that feeds the map widget if the meta tags aren't present.
    Same technique as backfill_coordinates.py, now run inline during the main scrape
    so new listings get coordinates immediately — no separate backfill pass needed."""
    try:
        lat_str = await page.get_attribute('meta[property="place:location:latitude"]', "content", timeout=2000)
        lng_str = await page.get_attribute('meta[property="place:location:longitude"]', "content", timeout=2000)
        if lat_str and lng_str:
            return float(lat_str), float(lng_str)
    except Exception:
        pass
    try:
        html = await page.content()
        m = re.search(r'"lat"\s*:\s*(-?\d+\.\d+)\s*,\s*"lng"\s*:\s*(-?\d+\.\d+)', html)
        if m:
            return float(m.group(1)), float(m.group(2))
    except Exception:
        pass
    return None, None


# ---------- STEALTH ----------
async def apply_stealth(page: Page) -> None:
    await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins',   { get: () => [1,2,3,4,5] });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
        window.chrome = { runtime: {} };
        const origQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (params) =>
            params.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : origQuery(params);
    """)

# ---------- HANDLE MODALS ----------
async def close_modals(page: Page):
    """Try to close any cookie consent / region / translation modal that blocks interaction."""
    try:
        close_selectors = [
            'button[aria-label*="Close" i]',
            'button:has-text("Accept")',
            'button:has-text("Accept all")',
            'button:has-text("OK")',
            'button:has-text("Continue")',
            'button:has-text("Got it")',
            '#onetrust-accept-btn-handler',   # common OneTrust cookie-consent button
            '[data-testid="modal-close"]',
        ]
        for sel in close_selectors:
            btn = page.locator(sel).first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click()
                await sleep_ms(500)
                break
    except:
        pass

    # Airbnb's own popovers (e.g. the "When" date picker that auto-opens after picking a
    # destination) render as a generic [data-testid="modal-container"] with no obvious
    # close button. That element is what was intercepting clicks on the "Where" field
    # ("... subtree intercepts pointer events"). Escape + a backdrop click clears it.
    try:
        modal = page.locator('[data-testid="modal-container"]').first
        if await modal.count() > 0 and await modal.is_visible():
            close_btn = modal.locator('button[aria-label*="Close" i]').first
            if await close_btn.count() > 0 and await close_btn.is_visible():
                await close_btn.click()
            else:
                await page.keyboard.press("Escape")
                await sleep_ms(300)
                # click a neutral corner of the page to drop focus/close any popover
                await page.mouse.click(5, 5)
            await sleep_ms(400)
    except:
        pass


async def dismiss_overlays(page: Page):
    """Escape key + close_modals combined — call before any click that keeps getting
    intercepted by a leftover cookie banner / date-picker popover."""
    try:
        await page.keyboard.press("Escape")
    except:
        pass
    await sleep_ms(200)
    await close_modals(page)


async def js_click(locator) -> bool:
    """Dispatch a native JS click directly on the element, bypassing Playwright's
    actionability/pointer-interception checks entirely. This is the fix for the
    'subtree intercepts pointer events' failures seen when clicking into the photo
    grid — an overlay div sits on top of the <img> so a real pointer-based click
    never lands, but a JS-dispatched click on the element still works fine because
    the browser doesn't care what's "on top" for a script-triggered click."""
    try:
        handle = await locator.element_handle(timeout=3000)
        if handle is None:
            return False
        await handle.evaluate("el => el.click()")
        return True
    except Exception:
        return False


async def robust_click(locator, page: Page, timeout: int = 8000, attempts: int = 3, clear_overlays: bool = True) -> bool:
    """Click with retries. By default each retry first clears whatever might be
    intercepting the click (cookie banner, stray popover). Pass clear_overlays=False for
    things like a destination-suggestion dropdown item, where pressing Escape would just
    close the dropdown you're trying to click into and make every retry fail."""
    last_err = None
    for _ in range(attempts):
        try:
            await locator.click(timeout=timeout)
            return True
        except Exception as e:
            last_err = e
            if clear_overlays:
                await dismiss_overlays(page)
            await sleep_ms(400)
    try:
        await locator.click(timeout=timeout, force=True)
        return True
    except Exception as e:
        print(f"  robust_click failed after {attempts} attempts + force: {e} (first error: {last_err})")
        return False


async def is_logged_in(page: Page) -> bool:
    # Guard: if we're still on a login/auth/signup URL we're definitely not logged in.
    # This was the primary bug — the hamburger-menu fallback below returned True
    # immediately because button[aria-label*="menu"] exists on /login before the user
    # has typed anything, so the poll loop exited on the very first check.
    try:
        current_url = page.url or ""
        if any(x in current_url for x in ["/login", "/authenticate", "/signup", "/oauth"]):
            return False
    except Exception:
        pass

    # A logged-OUT page always shows a "Log in" call-to-action somewhere in the header.
    login_cta = await page.locator('a:has-text("Log in"), button:has-text("Log in")').count()
    if login_cta > 0:
        return False

    positive_signals = [
        '[data-testid="header-profile-menu"]',
        'img[alt*="profile" i]',
        'button[aria-label*="profile" i]',
        'button[aria-label*="account" i]',
        'text=/Welcome back/i',
    ]
    for sel in positive_signals:
        if await page.locator(sel).count() > 0:
            return True

    # NOTE: the old hamburger-menu fallback was REMOVED. button[aria-label*="menu"]
    # exists on every Airbnb page including the unauthenticated /login page, so it
    # was a guaranteed false-positive that caused the wait loop to exit immediately.
    return False


async def wait_for_manual_continue(prompt: str) -> asyncio.Event:
    """Lets you press Enter in the console at any time to tell the script 'I'm logged
    in now, stop waiting on the automatic check and continue immediately'. Runs the
    blocking input() call on a background thread so it doesn't freeze the event loop."""
    event = asyncio.Event()
    loop = asyncio.get_running_loop()   # get_event_loop() is deprecated in 3.10+

    def _blocking_input():
        try:
            input(prompt)
        except Exception:
            pass
        loop.call_soon_threadsafe(event.set)

    loop.run_in_executor(None, _blocking_input)
    return event

def prompt_for_search_queries(default_queries: List[str]) -> List[str]:
    """Let the person typing at the console pick what gets searched instead of
    always sweeping the hardcoded ZIMBABWE_CITIES list. Enter blank -> keep the
    defaults; enter one place -> search just that; enter several separated by
    commas -> search all of them this run. Every price found still gets
    converted to USD the same way (see parse_price_text / CURRENT_ZAR_TO_USD_RATE).

    Skipped entirely when KARLON_SCRAPER_NONINTERACTIVE is set (e.g. when
    launched by karlon_supervisor.py) — under the supervisor this process's
    stdin is inherited and shared with the other managed processes, so a
    blocking input() here can sit waiting on a prompt nobody can reliably
    type into, holding the whole scrape (and therefore all fresh
    house_listings ingestion) hostage with no crash for the supervisor's
    restart logic to react to. Interactive manual runs are unaffected.
    """
    if os.environ.get("KARLON_SCRAPER_NONINTERACTIVE"):
        print(f"[airbnb_scraper] KARLON_SCRAPER_NONINTERACTIVE set — "
              f"using defaults without prompting: {', '.join(default_queries)}")
        return default_queries

    print("\n" + "=" * 60)
    print("🔎 SEARCH SETUP")
    print("=" * 60)
    print(f"Default (if you press Enter): {', '.join(default_queries)}")
    raw = input(
        "Enter a place to search (e.g. 'Victoria Falls' or 'Cape Town'), "
        "or several separated by commas, or press Enter for the defaults: "
    ).strip()
    if not raw:
        return default_queries
    queries = [q.strip() for q in raw.split(",") if q.strip()]
    return queries or default_queries


# ---------- SESSION REUSE ----------
async def try_restore_session(browser: Browser) -> Optional[BrowserContext]:
    """Reuse a previously saved login profile (cookies + localStorage) if it still works,
    so you don't have to log in by hand every run."""
    if not STORAGE_STATE_FILE.exists():
        return None

    print(f"Found saved login profile at {STORAGE_STATE_FILE} — checking if it's still valid...")
    context = await browser.new_context(
        storage_state=str(STORAGE_STATE_FILE),
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 900},
    )
    page = await context.new_page()
    await apply_stealth(page)
    try:
        await page.goto("https://www.airbnb.com/", wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        await dismiss_overlays(page)
        await sleep_ms(1500)
        if await is_logged_in(page):
            print("  Saved profile is still valid — skipping manual login.\n")
            await page.close()
            return context
        print("  Saved profile has expired — need to log in again.\n")
    except Exception as e:
        print(f"  Could not validate saved profile ({e}) — need to log in again.\n")

    await page.close()
    await context.close()
    return None


# ---------- LOGIN (fixed: domcontentloaded, overlay-safe clicks, saves profile) ----------
async def login_with_timeout(browser: Browser) -> Optional[BrowserContext]:
    print("\n=== LOGIN PHASE ===")
    print("Browser opened. Please log in to Airbnb manually. You have 3 minutes.")
    print("The script will continue once login is detected.")
    print(f"On success your session will be saved to: {STORAGE_STATE_FILE}")
    print("Subsequent runs will reuse that saved profile — no manual login needed.\n")

    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 900},
    )
    page = await context.new_page()
    await apply_stealth(page)

    try:
        await page.goto(
            "https://www.airbnb.com/login",
            wait_until="domcontentloaded",
            timeout=NAV_TIMEOUT
        )
    except Exception:
        print("  Login page load timed out. Trying via homepage...")
        try:
            await page.goto(
                "https://www.airbnb.com/",
                wait_until="domcontentloaded",
                timeout=NAV_TIMEOUT
            )
        except Exception as e:
            print(f"  Homepage load also failed: {e}")
        await dismiss_overlays(page)
        login_btn = page.locator('a:has-text("Log in"), button:has-text("Log in"), [data-testid="login-button"]').first
        if await login_btn.count() > 0:
            await robust_click(login_btn, page, timeout=10000)
            await page.wait_for_load_state("domcontentloaded", timeout=60000)

    print("  Tip: as soon as you see you're logged in, you can press Enter here")
    print("  to continue immediately instead of waiting for the automatic check.\n")
    manual_continue = await wait_for_manual_continue("")

    deadline = time.time() + LOGIN_TIMEOUT_SECONDS
    logged_in = False

    while time.time() < deadline:
        if manual_continue.is_set() or await is_logged_in(page):
            logged_in = True
            break
        await sleep_ms(2000)
        remaining = int(deadline - time.time())
        print(f"\r  Waiting for login... {remaining}s remaining (or press Enter)   ", end="")

    if not logged_in:
        print("\n  Login was not detected within the time limit.")
        await page.close()
        await context.close()
        return None

    print("\n  Login detected — saving session profile for reuse on next run...")
    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    await context.storage_state(path=str(STORAGE_STATE_FILE))
    if STORAGE_STATE_FILE.exists() and STORAGE_STATE_FILE.stat().st_size > 0:
        print(f"  ✅ Profile saved to: {STORAGE_STATE_FILE}")
        print("  Next run will skip login entirely and use this saved session.\n")
    else:
        print("  ⚠️  Profile file was not written — you may need to log in again next run.\n")
    await page.close()
    return context

# ---------- DOWNLOAD IMAGE ----------
MIN_VALID_IMAGE_BYTES = 5000  # a real listing photo is always far bigger than this;
                               # tracking pixels / tiny icon badges are a few dozen bytes

async def download_image(url: str, filepath: Path, sem: asyncio.Semaphore) -> bool:
    async with sem:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=30) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if len(data) < MIN_VALID_IMAGE_BYTES:
                            print(f"  Skipping tiny/placeholder image ({len(data)} bytes): {url}")
                            return False
                        filepath.write_bytes(data)
                        return True
                    else:
                        print(f"  Image download failed: HTTP {resp.status} for {url}")
                        return False
        except Exception as e:
            print(f"  Image download error: {e} for {url}")
            return False

# ---------- SCRAPE WORKER ----------
async def scrape_worker(
    task_queue: asyncio.Queue,
    result_queue: asyncio.Queue,
    context: BrowserContext,
    checkpoint: Checkpoint,
    image_sem: asyncio.Semaphore,
    days_from_now: int = CHECKIN_DAYS_FROM_NOW,
    nights: int = STAY_NIGHTS,
):
    while True:
        item = await task_queue.get()
        if item is None:
            task_queue.task_done()
            break
        url, city_hint = item if isinstance(item, tuple) else (item, "")

        if url in checkpoint.scraped_urls:
            print(f"⏭️  Already scraped: {url}")
            task_queue.task_done()
            continue

        page = await context.new_page()
        try:
            await apply_stealth(page)
            print(f"🔄 Scraping [{city_hint or '?'}]: {url}")
            data = await scrape_listing(page, url, image_sem, city_hint, days_from_now=days_from_now, nights=nights)
            if data:
                location_text = str(data.get("location") or "").lower()
                if any(hint in location_text for hint in DISALLOWED_COUNTRY_HINTS):
                    print(f"⛔ Skipping {url} — listing location '{data.get('location')}' "
                          f"looks non-Zimbabwean, not saving (source: {city_hint or '?'}).")
                    task_queue.task_done()
                    continue
                folder_path = await save_listing_data(data, image_sem)
                await result_queue.put((data, folder_path))
                checkpoint.scraped_urls.add(url)
                checkpoint.save()
                print(f"✅ Scraped: {url} -> {folder_path}")
        except Exception as e:
            print(f"❌ Error scraping {url}: {e}")
        finally:
            await page.close()
            task_queue.task_done()
            await random_delay()

async def safe_inner_text(locator, timeout=5000, default=""):
    try:
        return await locator.inner_text(timeout=timeout)
    except:
        return default

async def get_raw_price_text(page: Page) -> str:
    """The booking widget's price line, raw. Preferred: the smallest element
    around the "for N nights" text that also holds the amount ("$3,172 for 22
    nights") — the amount and the night count are separate elements, and
    taking the amount alone lost the night count. Fallback: the first SHORT
    element matching a price. Parsing happens in parse_price_text()."""
    try:
        nights_el = page.locator("text=/for \\d+ nights?/i").first
        if await nights_el.count() > 0:
            node = nights_el
            for _ in range(3):
                try:
                    txt = re.sub(r"\s+", " ", await node.inner_text(timeout=1200)).strip()
                except Exception:
                    break
                if re.search(r"(\$|R\s?)\d", txt) and NIGHTS_RE.search(txt) and len(txt) <= 150:
                    return txt
                node = node.locator("xpath=..")
    except Exception:
        pass
    try:
        candidates = page.locator("text=/\\$[0-9]+|R\\s?[0-9,]+|for \\d+ night/i")
        count = await candidates.count()
        for i in range(min(count, 20)):
            try:
                txt = await candidates.nth(i).inner_text(timeout=1200)
            except Exception:
                continue
            txt = re.sub(r"\s+", " ", txt).strip()
            if 0 < len(txt) <= 80:
                return txt
    except Exception:
        pass
    return ""


# Harare suburbs, for naming where a listing is. Airbnb shows the exact
# address only after booking and offsets the map pin by a few hundred metres,
# so a suburb is the most precise honest answer: taken from the host's own
# words when the description names one ("a charming Greendale apartment"),
# else from OpenStreetMap for the (approximate) pin.
HARARE_SUBURBS = [
    "Alexandra Park", "Arcadia", "Ashdown Park", "Athlone", "Avondale", "Avonlea", "Ballantyne Park",
    "Belgravia", "Belvedere", "Bluff Hill", "Borrowdale", "Borrowdale Brooke", "Borrowdale West",
    "Braeside", "Carrick Creagh", "Chisipite", "Colne Valley", "Cranborne", "Eastlea", "Emerald Hill",
    "Glen Lorne", "Gletwyn", "Greendale", "Greystone Park", "Groombridge", "Gun Hill", "Hatfield",
    "Helensvale", "Highfield", "Highlands", "Hillside", "Hogerty Hill", "Kensington", "Lewisam",
    "Logan Park", "Mabelreign", "Mandara", "Manresa", "Marlborough", "Milton Park", "Monavale",
    "Mount Pleasant", "Mount Pleasant Heights", "Msasa", "Newlands", "Northwood", "Pomona",
    "Prospect", "Quinnington", "Rhodesville", "Ridgeview", "Rolf Valley", "Ruwa", "Sentosa",
    "Southerton", "Strathaven", "Tynwald", "Vainona", "Waterfalls", "Westgate", "Workington",
    "Chitungwiza", "Epworth", "Mabvuku", "Tafara", "Kuwadzana", "Warren Park", "Budiriro", "Glen View",
]
_SUBURB_RES = [(name, re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE))
               for name in sorted(HARARE_SUBURBS, key=len, reverse=True)]


def find_suburb(*texts: str) -> str:
    """The first known suburb named in the given texts (longest names win,
    so "Borrowdale Brooke" beats "Borrowdale"), or ""."""
    blob = "\n".join(t for t in texts if t)
    for name, rx in _SUBURB_RES:
        if rx.search(blob):
            return name
    return ""


def extract_capacity(text: str) -> str:
    """'6 guests · 3 bedrooms · 3 beds · 2.5 baths' from the listing page."""
    m = re.search(r"\b(\d+\+?\s+guests?)\s*[·•]\s*([^\n]{0,80})", text or "", re.IGNORECASE)
    if not m:
        return ""
    return (m.group(1) + " · " + m.group(2)).strip(" ·")


def listing_overview(text: str) -> str:
    """The top of the listing page: from the capacity line down to "What this
    place offers" / "Where you'll be" — title, highlights and the visible
    description, but not the map, reviews or "similar listings" further down
    (which name other suburbs)."""
    text = text or ""
    m = re.search(r"\d+\+?\s+guests?", text, re.IGNORECASE)
    start = m.start() if m else 0
    stops = [i for i in (text.find("What this place offers", start), text.find("Where you\u2019ll be", start),
                         text.find("Where you'll be", start)) if i > 0]
    return text[start:min(stops)] if stops else text[start:start + 3000]


_geocode_cache: Dict[tuple, str] = {}
_geocode_lock = asyncio.Lock()
_geocode_last = 0.0


async def reverse_geocode_suburb(lat: float, lng: float) -> str:
    """Suburb for a map pin from OpenStreetMap Nominatim (free; its policy is
    max 1 request/second with an identifying User-Agent, both respected).
    Cached per ~100 m. Best effort: "" on any failure."""
    global _geocode_last
    key = (round(lat, 3), round(lng, 3))
    if key in _geocode_cache:
        return _geocode_cache[key]
    async with _geocode_lock:
        wait = 1.1 - (time.time() - _geocode_last)
        if wait > 0:
            await asyncio.sleep(wait)
        _geocode_last = time.time()
        try:
            import aiohttp
            session = await _karlon_http()
            async with session.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"format": "jsonv2", "lat": lat, "lon": lng, "zoom": 16, "addressdetails": 1},
                headers={"User-Agent": "KarlonScraper/1.0 (KARLCON listings)"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                addr = (await resp.json()).get("address", {}) if resp.status == 200 else {}
        except Exception:
            addr = {}
    name = (addr.get("suburb") or addr.get("neighbourhood") or addr.get("quarter")
            or addr.get("city_district") or "")
    _geocode_cache[key] = name
    return name


async def scrape_listing(
    page: Page,
    url: str,
    image_sem: asyncio.Semaphore,
    city_hint: str = "",
    days_from_now: int = CHECKIN_DAYS_FROM_NOW,
    nights: int = STAY_NIGHTS,
) -> Optional[Dict]:
    # Go straight in with check-in/check-out dates baked into the URL — without them
    # Airbnb frequently shows "Add dates to see prices" instead of a real number.
    dated_url = dated_listing_url(url, days_from_now=days_from_now, nights=nights)
    await page.goto(dated_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    await random_delay()
    await close_modals(page)

    listing_id = re.search(r"rooms/(\d+)", url)
    listing_id = listing_id.group(1) if listing_id else "unknown"

    title = await safe_inner_text(page.locator("h1").first)

    subtitle = ""
    try:
        subtitle = await page.locator("h1").first.locator("xpath=following::*[self::h2 or self::div][1]").inner_text(timeout=3000)
    except:
        pass
    location = ""
    loc_match = re.search(r"in ([A-Za-z0-9,\s]+)$", subtitle, re.I)
    if loc_match:
        location = loc_match.group(1).strip()
    if not location:
        location = subtitle.strip()

    # Coordinates — Open Graph place:location meta tags, same technique as
    # backfill_coordinates.py, now captured inline so there's no separate pass needed.
    lat, lng = await scrape_coordinates(page)

    # Price: dates are already in the URL, but some listings ignore that and still
    # show "Add dates for prices" — fall back to clicking the booking-widget calendar
    # directly (same as repair_prices.py's fill_dates_via_widget) before giving up.
    raw_price = await get_raw_price_text(page)
    if not raw_price:
        if await fill_dates_via_widget(page):
            await close_modals(page)
            raw_price = await get_raw_price_text(page)
    price_detail = parse_price_text(raw_price, expected_nights=nights)
    price = format_price_for_index(price_detail)

    rating = ""
    reviews_count = ""
    try:
        rating_block = await page.locator("text=/Reviews?/i").first.locator("xpath=..").inner_text(timeout=3000)
        r_match = re.search(r"([\d.]+)", rating_block)
        c_match = re.search(r"(\d+)\s*Reviews?", rating_block, re.I)
        if r_match:
            rating = r_match.group(1)
        if c_match:
            reviews_count = c_match.group(1)
    except:
        pass

    description = ""
    try:
        show_more = page.locator('button:has-text("Show more"), a:has-text("Show more")').first
        if await show_more.count() > 0:
            await show_more.click(timeout=5000)
            await random_delay()
            modal = page.locator('[role="dialog"], div:has-text("About this space")').first
            description = await safe_inner_text(modal)
            close_btn = page.locator('[role="dialog"] button[aria-label*="Close" i]').first
            if await close_btn.count() > 0:
                await close_btn.click()
            else:
                await page.keyboard.press("Escape")
            await random_delay()
    except:
        pass

    amenities = await scrape_amenities(page)
    host = await scrape_host(page)
    contact_url = await get_contact_url(page)

    things_to_know = ""
    try:
        ttk = page.locator("text=/Things to know/i").first.locator("xpath=ancestor::*[self::section][1]")
        things_to_know = await safe_inner_text(ttk)
    except:
        pass

    image_urls = await collect_images(page)

    capacity, overview = "", ""
    try:
        main_text = await page.locator("main").first.inner_text(timeout=4000)
        capacity = extract_capacity(main_text)
        overview = listing_overview(main_text)
    except Exception:
        pass
    neighbourhood = find_suburb(description, overview, title, subtitle)
    if not neighbourhood and lat is not None and lng is not None:
        neighbourhood = await reverse_geocode_suburb(lat, lng)

    return {
        "listingId": listing_id,
        "url": url,
        "datedUrl": dated_url,
        "searchCity": city_hint,
        "title": title.strip(),
        "subtitle": subtitle.strip(),
        "location": location,
        "lat": lat,
        "lng": lng,
        "price": price,
        "priceDetail": price_detail,
        "rating": rating,
        "reviewsCount": reviews_count,
        "capacity": capacity,
        "neighbourhood": neighbourhood,
        "description": description.strip(),
        "amenities": amenities,
        "host": host,
        "contactUrl": contact_url,
        "thingsToKnow": things_to_know.strip(),
        "images": image_urls,
        "scrapedAt": now_iso(),
        # Exact dates this page was actually priced for (see dated_url above) —
        # index_worker reads these back off to tell push_listing_to_karlon the
        # real window, instead of it always assuming CHECKIN_DAYS_FROM_NOW/STAY_NIGHTS.
        "requestedDaysFromNow": days_from_now,
        "requestedNights": nights,
    }

async def scrape_amenities(page: Page) -> List[str]:
    amenities = []
    try:
        show_btn = page.locator('button:has-text("Show all amenities"), button:has-text("amenities")').first
        if await show_btn.count() > 0:
            await show_btn.click(timeout=5000)
            await random_delay()
            modal = page.locator('[role="dialog"]').first
            await modal.wait_for(state="visible", timeout=5000)
            items = await modal.locator("div, li, span").all_inner_texts()
            close_btn = modal.locator('button[aria-label*="Close" i]').first
            if await close_btn.count() > 0:
                await close_btn.click()
            else:
                await page.keyboard.press("Escape")
            amenities.extend([t.strip() for t in items if 2 < len(t.strip()) < 60])
        else:
            section = page.locator('section:has-text("amenities"), div:has-text("amenities")').first
            items = await section.locator("div, li").all_inner_texts()
            amenities.extend([t.strip() for t in items if 2 < len(t.strip()) < 60])
    except:
        pass
    return list(set(amenities))

async def scrape_host(page: Page) -> Dict:
    host = {"name": "", "superhost": False, "yearsHosting": "", "reviews": "", "rating": "", "bio": ""}
    try:
        host_section = page.locator("text=/Hosted by /i").first.locator("xpath=ancestor::*[self::div][3]")
        host_text = await host_section.inner_text(timeout=5000)
        name_match = re.search(r"Hosted by ([A-Za-z\u00C0-\u024F' -]+)", host_text, re.I)
        if name_match:
            host["name"] = name_match.group(1).strip()
        host["superhost"] = "Superhost" in host_text
        yrs_match = re.search(r"(\d+\s*Years? hosting)", host_text, re.I) or re.search(r"(\d+\s*Months? hosting)", host_text, re.I)
        if yrs_match:
            host["yearsHosting"] = yrs_match.group(1)
        rev_match = re.search(r"(\d+)\s*Reviews?", host_text, re.I)
        if rev_match:
            host["reviews"] = rev_match.group(1)
        rat_match = re.search(r"([\d.]+)\s*(?:\u2605|\u2B50)?\s*Rating", host_text, re.I)
        if rat_match:
            host["rating"] = rat_match.group(1)
    except:
        pass
    try:
        bio_block = page.locator("text=/Meet your host/i").first.locator("xpath=ancestor::*[self::section][1]")
        host["bio"] = await safe_inner_text(bio_block)
    except:
        pass
    return host

async def get_contact_url(page: Page) -> str:
    contact = ""
    try:
        contact_link = page.locator('a[href*="/contact_host/"], a[href*="/send_message"]').first
        if await contact_link.count() > 0:
            href = await contact_link.get_attribute("href")
            contact = href if href.startswith("http") else f"https://www.airbnb.com{href}"
        else:
            msg_btn = page.locator('button:has-text("Message host")').first
            if await msg_btn.count() > 0:
                async with page.context.expect_page() as new_page_info:
                    await msg_btn.click()
                new_page = await new_page_info.value
                contact = new_page.url
                await new_page.close()
    except:
        pass
    return contact

async def _in_lightbox(page: Page) -> bool:
    """Return True if the swipeable lightbox with an 'X / N' counter is currently open."""
    # Broad match — no anchors so it works even if the element contains surrounding
    # whitespace or extra text nodes (Playwright's text= selector does substring match
    # by default; the regex just needs to find the pattern anywhere in the text).
    try:
        counter = page.locator('[role="dialog"]').locator("text=/\\d+\\s*\\/\\s*\\d+/").first
        return await counter.count() > 0
    except:
        return False


async def open_photo_viewer(page: Page) -> bool:
    """Open the swipeable '1 / N' lightbox.

    Airbnb has two gallery surfaces:
      A) 'Show all photos' button  → sometimes opens a scrollable grid ('Photo tour'),
         sometimes opens the lightbox directly.
      B) Clicking the hero banner image → same ambiguity.

    Either way, if we land on the grid first we need a second click on one of the
    photos inside it to reach the actual swipeable lightbox.  This function handles
    both paths and always confirms the counter is visible before returning True.
    """

    async def _enter_lightbox_from_grid() -> bool:
        """We're on the Photo-tour grid inside a dialog — click the first real photo."""
        dialog = page.locator('[role="dialog"]').first
        scope  = dialog if await dialog.count() > 0 else page
        # Prefer images that are definitely listing photos (airbnb CDN src)
        first_photo = scope.locator('img[src*="airbnb"]').first
        if await first_photo.count() == 0:
            first_photo = scope.locator("img").first  # last resort
        if await first_photo.count() == 0:
            return False
        await first_photo.scroll_into_view_if_needed(timeout=5000)
        clicked = await js_click(first_photo)
        if not clicked:
            clicked = await robust_click(first_photo, page, attempts=2, clear_overlays=False)
        await sleep_ms(1200)
        return await _in_lightbox(page)

    # ── Path A: the explicit "Show all photos" button ──────────────────────────
    # The button sits at the BOTTOM of the hero photo grid and is often below the
    # initial viewport — scroll it into view before trying to click it.
    # We first ask JS to find and scroll the button, then let Playwright click it.
    show_all_handle = await page.evaluate_handle("""() => {
        // Try multiple text / attribute patterns the button uses across locales
        const patterns = [
            'button:has-text("Show all photos")',
            '[data-testid="photo-viewer-section-button"]',
            'button[aria-label*="photos" i]',
        ];
        // querySelectorAll via CSS (no :has-text in native CSS) — iterate all buttons
        const allBtns = Array.from(document.querySelectorAll('button, [role="button"]'));
        for (const pat of ['Show all photos', 'all photos', 'photos']) {
            const match = allBtns.find(el =>
                el.textContent && el.textContent.toLowerCase().includes(pat.toLowerCase())
            );
            if (match) {
                match.scrollIntoView({ block: 'center' });
                return match;
            }
        }
        // data-testid fallback
        const byTestId = document.querySelector(
            '[data-testid="photo-viewer-section-button"], [data-section-id*="photo"]'
        );
        if (byTestId) { byTestId.scrollIntoView({ block: 'center' }); return byTestId; }
        return null;
    }""")
    show_all_el = show_all_handle.as_element()
    if show_all_el:
        show_all = page.locator("xpath=//button[contains(translate(., 'PHOTOS', 'photos'), 'photos')]").first
        # Use the element handle directly for a reliable click
        try:
            try:
                await show_all_el.click(timeout=8000)
            except Exception:
                # Real pointer click failed (likely intercepted by an overlay) —
                # fall back to a JS-dispatched click which ignores what's on top.
                await show_all_el.evaluate("el => el.click()")
            await sleep_ms(1200)
            if await _in_lightbox(page):
                return True
            if await _enter_lightbox_from_grid():
                return True
        except Exception as e:
            print(f"  Path A click failed: {e}")
    else:
        # Playwright locator fallback (in case evaluate_handle returned null)
        show_all = page.locator(
            'button:has-text("Show all photos"), '
            'button:has-text("all photos"), '
            '[data-testid="photo-viewer-section-button"]'
        ).first
        if await show_all.count() > 0:
            if await robust_click(show_all, page, attempts=2, clear_overlays=True):
                await sleep_ms(1200)
                if await _in_lightbox(page):
                    return True
                if await _enter_lightbox_from_grid():
                    return True

    # ── Path B: click a large hero-grid photo directly ────────────────────────
    # Use JS to find the first visible image with meaningful natural dimensions —
    # avoids the 132px lazy-placeholder that `main img[src*="airbnb"]` was resolving
    # to and triggering "Element is outside of the viewport" failures.
    hero_handle = await page.evaluate_handle("""() => {
        const imgs = Array.from(document.querySelectorAll('img'));
        // Find the first large, rendered, airbnb-CDN image
        return imgs.find(img =>
            img.src.includes('airbnb') &&
            !img.src.includes('data:image') &&
            img.naturalWidth >= 300 &&        // real photo, not icon
            img.getBoundingClientRect().width >= 100   // actually visible
        ) || null;
    }""")
    hero_el = hero_handle.as_element()
    if hero_el:
        try:
            try:
                await hero_el.click(timeout=8000)
            except Exception:
                await hero_el.evaluate("el => el.click()")
            await sleep_ms(1200)
            if await _in_lightbox(page):
                return True
            if await _enter_lightbox_from_grid():
                return True
        except Exception as e:
            print(f"  Path B (JS large-img) failed: {e}")

    # ── Path C: explicit testid / aria selectors ──────────────────────────────
    for sel in [
        '[data-testid="photo-viewer-trigger"]',
        'button[aria-label*="View photo" i]',
        'div[data-testid*="hero"] img[src*="airbnb"]',
        'div[data-testid*="photo"] img[src*="airbnb"]',
    ]:
        el = page.locator(sel).first
        if await el.count() > 0:
            if await robust_click(el, page, attempts=2, clear_overlays=False):
                await sleep_ms(1200)
                if await _in_lightbox(page):
                    return True
                if await _enter_lightbox_from_grid():
                    return True
                break

    return False


async def collect_images_via_viewer(page: Page, min_images: int = MIN_IMAGES_PER_LISTING) -> List[str]:
    """Open the '1 / N' lightbox then click Next until every photo has been grabbed.

    Key fixes vs previous version
    ──────────────────────────────
    • Dialog scoped by data-testid first, counter-text second — avoids matching
      unrelated dialogs (cookie banners, login prompts).
    • Next-button selector is DIALOG-SCOPED, so it can never accidentally match
      the calendar's "Move forward to switch to the next month" control that also
      contains the substring "next" and lives off-screen on the search page.
    • No image cap — loops until the counter says we've reached the last photo,
      or (when no counter is visible) until Next stops surfacing new images AND
      we've already met MIN_IMAGES_PER_LISTING.
    • Prefers img[src*="airbnb"] inside the dialog to skip avatar / icon imgs.
    """
    image_urls: List[str] = []

    if not await open_photo_viewer(page):
        return image_urls

    await sleep_ms(800)

    # ── Find the lightbox dialog ───────────────────────────────────────────────
    # Priority 1: Airbnb's own photo-viewer testid
    dialog = page.locator('[data-testid*="photo-viewer"], [data-testid*="lightbox"]').first
    if await dialog.count() == 0:
        # Priority 2: any dialog that contains the X/N counter text
        for d in await page.locator('[role="dialog"]').all():
            try:
                txt = await d.inner_text(timeout=1000)
                if re.search(r"\d+\s*/\s*\d+", txt):
                    dialog = d
                    break
            except:
                pass
    if await dialog.count() == 0:
        # Last resort: first open dialog
        dialog = page.locator('[role="dialog"]').first
    if await dialog.count() == 0:
        dialog = page  # fully unscoped fallback

    # ── Read total from counter ────────────────────────────────────────────────
    total = None
    try:
        # Use a plain CSS/text combo — no anchors, so whitespace differences don't bite
        all_text_nodes = await dialog.locator("text=/\\d+ \\/ \\d+/").all_inner_texts()
        if not all_text_nodes:
            all_text_nodes = await dialog.locator("text=/\\d+\\/\\d+/").all_inner_texts()
        for txt in all_text_nodes:
            m = re.search(r"(\d+)\s*/\s*(\d+)", txt)
            if m:
                total = int(m.group(2))
                print(f"  Gallery total from counter: {total} photos")
                break
    except:
        pass

    # ── DIALOG-SCOPED Next button ─────────────────────────────────────────────
    # Must be scoped to the dialog — the calendar's "Move forward to next month"
    # button also matches aria-label*="Next" but sits off-screen, causing
    # "Element is outside of the viewport" failures on every click.
    next_btn = dialog.locator(
        'button[aria-label*="Next photo" i], '
        'button[aria-label*="next photo" i], '
        'button[aria-label="Next"]'          # exact label used in current Airbnb build
    ).first

    seen: set = set()
    i = 0
    # hard_ceiling = actual total when known; 300 safety-valve otherwise
    hard_ceiling = total if total else 300

    while i < hard_ceiling:
        # Grab the current photo's src — prefer airbnb-CDN images
        try:
            img = dialog.locator('img[src*="airbnb"]').first
            if await img.count() == 0:
                img = dialog.locator("img").first
            src = await img.get_attribute("src", timeout=3000)
            if src and src not in seen:
                seen.add(src)
                image_urls.append(src)
                print(f"  Photo {len(image_urls)}{f'/{total}' if total else ''} captured")
        except:
            pass

        # Exit conditions
        if total and i >= total - 1:
            break
        if await next_btn.count() == 0:
            break

        before = len(seen)
        clicked = await robust_click(next_btn, page, attempts=3, clear_overlays=False)
        if not clicked:
            break
        await sleep_ms(450)
        i += 1

        # No counter available: stop if Next stopped delivering new images AND
        # we've already passed the minimum floor.
        if not total and len(image_urls) >= min_images and len(seen) == before:
            print("  Gallery appears to have looped — stopping.")
            break

    # ── Close the lightbox ────────────────────────────────────────────────────
    try:
        close_btn = dialog.locator('button[aria-label*="Close" i]').first
        if await close_btn.count() > 0:
            await close_btn.click(timeout=3000)
        else:
            await page.keyboard.press("Escape")
    except:
        pass
    await random_delay()

    return image_urls


async def collect_images_via_direct_nav(page: Page) -> List[str]:
    """Strategy 0 — skip clicking into the gallery entirely.

    Airbnb's photo tour is reachable by appending ?modal=PHOTO_TOUR_SCROLLABLE to the
    listing URL directly (confirmed from the browser: this lands on the same
    full-page scrollable gallery as clicking 'Show all photos', organized by room).
    This sidesteps every overlay-interception problem the click-based paths hit,
    because we never click anything to get there.

    Each <img> in that view carries a data-original-uri attribute pointing at the
    FULL resolution source (…/pictures/hosting/<id>/origin.jpg), which is better
    than the resized ?im_w=1200 src the other strategies fall back to.
    """
    image_urls: List[str] = []
    original_url = page.url.split("?")[0].split("#")[0]
    tour_url = original_url + "?modal=PHOTO_TOUR_SCROLLABLE"

    try:
        await page.goto(tour_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        await random_delay()
        await close_modals(page)

        # This is a full page (not a small dialog) — scroll the whole document to
        # trigger lazy-loading of every room's photos, same pattern as scroll_results.
        last_count = -1
        stable_rounds = 0
        for _ in range(60):  # hard ceiling so a stuck page can't loop forever
            await page.evaluate("window.scrollBy(0, Math.round(window.innerHeight * 0.85))")
            await sleep_ms(450)
            count = await page.evaluate(
                "document.querySelectorAll('img[data-original-uri], img[src*=\"pictures/hosting\"]').length"
            )
            if count == last_count:
                stable_rounds += 1
                if stable_rounds >= 3:
                    break
            else:
                stable_rounds = 0
            last_count = count
            at_bottom = await page.evaluate(
                "window.innerHeight + window.scrollY >= document.body.scrollHeight - 50"
            )
            if at_bottom:
                break

        image_urls = await page.evaluate("""() => {
            const urls = new Set();
            document.querySelectorAll('img').forEach(img => {
                const orig = img.getAttribute('data-original-uri');
                if (orig && orig.includes('/pictures/hosting/')) { urls.add(orig); return; }
                const srcset = img.getAttribute('srcset');
                if (srcset) {
                    let best = null, bestW = 0;
                    srcset.split(',').forEach(part => {
                        const bits = part.trim().split(' ');
                        const u = bits[0];
                        const w = bits[1] ? parseInt(bits[1]) : 0;
                        if (u && u.includes('/pictures/hosting/') && w > bestW) { best = u; bestW = w; }
                    });
                    if (best) { urls.add(best); return; }
                }
                const src = img.getAttribute('src');
                if (src && src.includes('/pictures/hosting/') && !src.startsWith('data:image')) {
                    urls.add(src);
                }
            });
            return Array.from(urls);
        }""")
        print(f"  Strategy 0 (direct photo-tour nav): {len(image_urls)} images")
    except Exception as e:
        print(f"  Strategy 0 failed: {e}")
    finally:
        # Go back to the plain listing URL so nothing downstream (or a re-run of
        # collect_images on the same page) is left sitting on the gallery view.
        try:
            await page.goto(original_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            await random_delay()
        except Exception:
            pass

    return image_urls


async def collect_images(page: Page) -> List[str]:
    """Collect listing images using four strategies in priority order.

    Strategy 0 (direct nav to the photo-tour URL) is tried first and is usually
    sufficient on its own — it needs no clicking, so it can't be blocked by the
    overlay-interception issue that broke Strategies 1/2. Strategies 1-3 only run
    if Strategy 0 came up short.
    """
    image_urls: set = set()

    # ── Strategy 0: direct navigation to the photo-tour modal URL ─────────────
    try:
        direct_imgs = await collect_images_via_direct_nav(page)
        image_urls.update(direct_imgs)
    except Exception as e:
        print(f"  Strategy 0 failed: {e}")

    # ── Strategy 1: lightbox pager (1/N counter + Next button) ───────────────
    if len(image_urls) < MIN_IMAGES_PER_LISTING:
        try:
            viewer_imgs = await collect_images_via_viewer(page)
            image_urls.update(viewer_imgs)
            print(f"  Strategy 1 (lightbox): {len(viewer_imgs)} images")
        except Exception as e:
            print(f"  Strategy 1 failed: {e}")

    # ── Strategy 2: 'Show all photos' scrollable grid ─────────────────────────
    # Run whenever Strategy 1 came up short — not just when it returned nothing.
    if len(image_urls) < MIN_IMAGES_PER_LISTING:
        try:
            gallery_btn = page.locator(
                'button:has-text("Show all photos"), '
                '[data-testid="photo-viewer-section-button"], '
                'button[aria-label*="Show all photos" i]'
            ).first
            if await gallery_btn.count() > 0:
                try:
                    await gallery_btn.click(timeout=5000)
                except Exception:
                    await js_click(gallery_btn)
                await random_delay()
                gallery_modal = page.locator('[role="dialog"], [data-testid*="photo-tour"]').first
                await gallery_modal.wait_for(state="visible", timeout=8000)

                # Scroll through the grid to lazy-load all thumbnails
                scroll_height = await gallery_modal.evaluate("el => el.scrollHeight")
                pos = 0
                while pos < scroll_height:
                    pos = min(pos + 900, scroll_height)
                    await gallery_modal.evaluate("(el, y) => el.scrollTo(0, y)", pos)
                    await sleep_ms(400)
                    new_h = await gallery_modal.evaluate("el => el.scrollHeight")
                    if new_h > scroll_height:
                        scroll_height = new_h

                imgs = await page.evaluate("""() => {
                    const urls = new Set();
                    document.querySelectorAll(
                        '[role="dialog"] img, [data-testid*="photo-tour"] img'
                    ).forEach(img => {
                        const src    = img.getAttribute('src');
                        const srcset = img.getAttribute('srcset');
                        if (src && src.includes('/pictures/hosting/')) urls.add(src);
                        if (srcset) {
                            srcset.split(',').forEach(part => {
                                const u = part.trim().split(' ')[0];
                                if (u && u.includes('/pictures/hosting/')) urls.add(u);
                            });
                        }
                    });
                    return Array.from(urls).filter(u => !u.includes('data:image'));
                }""")
                image_urls.update(imgs)
                print(f"  Strategy 2 (grid): {len(imgs)} images")

                close_btn = page.locator('[role="dialog"] button[aria-label*="Close" i]').first
                if await close_btn.count() > 0:
                    await close_btn.click()
                else:
                    await page.keyboard.press("Escape")
                await random_delay()
        except Exception as e:
            print(f"  Strategy 2 failed: {e}")

    # ── Strategy 3: raw DOM scrape (last resort — may include thumbnails) ─────
    if len(image_urls) < MIN_IMAGES_PER_LISTING:
        try:
            dom_imgs = await page.evaluate("""() => {
                const urls = new Set();
                document.querySelectorAll('img').forEach(img => {
                    const src    = img.getAttribute('src');
                    const srcset = img.getAttribute('srcset');
                    if (src && src.includes('/pictures/hosting/')) urls.add(src);
                    if (srcset) {
                        srcset.split(',').forEach(part => {
                            const u = part.trim().split(' ')[0];
                            if (u && u.includes('/pictures/hosting/')) urls.add(u);
                        });
                    }
                });
                return Array.from(urls).filter(u => !u.includes('data:image'));
            }""")
            image_urls.update(dom_imgs)
            print(f"  Strategy 3 (DOM): {len(dom_imgs)} images")
        except Exception as e:
            print(f"  Strategy 3 failed: {e}")

    # ── Deduplicate: for each canonical URL keep the longest (highest-res) variant ──
    # KEY FIX: strip ONLY query-string resize params (?im_w=720, ?im_policy=...)
    # The old regex re.sub(r"/im/[^?]+", "", u) removed the entire image path,
    # collapsing every Airbnb CDN image to the same root "https://a0.muscache.com"
    # so all N images became 1 key and only 1-2 images survived dedup.
    seen: dict = {}
    for u in image_urls:
        key = u.split("?")[0]           # canonical path, no resize params
        if key not in seen or len(u) > len(seen[key]):
            seen[key] = u

    result = list(seen.values())
    print(f"  Total unique images after dedup: {len(result)}")

    # ── Apply cap — CRITICAL: when MAX_IMAGES_PER_LISTING == 0, [:0] == [] ───
    # So we must guard explicitly.  0 means "no cap — download everything".
    if MAX_IMAGES_PER_LISTING and MAX_IMAGES_PER_LISTING > 0:
        result = result[:MAX_IMAGES_PER_LISTING]

    return result

async def save_listing_data(data: Dict, image_sem: asyncio.Semaphore) -> Path:
    folder_name = sanitize_folder_name(f"{data['location'] or data['title']}_{data['listingId']}")
    folder_path = Path(OUTPUT_DIR) / folder_name
    images_path = folder_path / "images"
    images_path.mkdir(parents=True, exist_ok=True)

    (folder_path / "info.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    img_tasks = []
    for idx, img_url in enumerate(data["images"], 1):
        ext = re.search(r"\.(jpg|jpeg|png|webp)", img_url, re.I)
        ext = ext.group(1) if ext else "jpg"
        filename = f"image_{idx:03d}.{ext}"
        filepath = images_path / filename
        img_tasks.append(download_image(img_url, filepath, image_sem))
    if img_tasks:
        await asyncio.gather(*img_tasks)

    return folder_path

# ---------- MESSAGE WORKER ----------
async def message_worker(msg_queue: asyncio.Queue, context: BrowserContext, checkpoint: Checkpoint):
    while True:
        item = await msg_queue.get()
        if item is None:
            msg_queue.task_done()
            break
        url, contact_url = item

        if url in checkpoint.sent_urls:
            print(f"⏭️  Already messaged: {url}")
            msg_queue.task_done()
            continue

        page = await context.new_page()
        try:
            await apply_stealth(page)
            print(f"💬 Messaging: {url}")
            success = await send_message_to_host(page, url, contact_url)
            if success:
                checkpoint.sent_urls.add(url)
                checkpoint.save()
                print(f"✅ Message sent to: {url}")
            else:
                print(f"❌ Failed to send message to: {url}")
        except Exception as e:
            print(f"❌ Error messaging {url}: {e}")
        finally:
            await page.close()
            msg_queue.task_done()
            await random_delay()

# ---------- CONTACT-HOST DATE SELECTION ----------
# Fixed check-in for every outgoing message. Checkout rotates through a fixed
# candidate pool (excluding Saturdays — those kept showing "This date is
# unavailable" in testing) so not every message to every host uses the same
# checkout day.
CONTACT_CHECKIN_DATE = datetime(2027, 8, 8).date()

def _contact_checkout_candidates(checkin) -> List:
    """Candidate checkout dates: the week following check-in, skipping Saturdays."""
    return [checkin + timedelta(days=n) for n in range(1, 8) if (checkin + timedelta(days=n)).weekday() != 5]

_contact_checkout_cycle = cycle(_contact_checkout_candidates(CONTACT_CHECKIN_DATE))

def next_contact_checkout_date():
    """Pull the next checkout date off the rotation. Safe to call from concurrent
    message workers — itertools.cycle's __next__ is a single synchronous step with
    no 'await' inside it, so two workers can't interleave mid-call under asyncio's
    cooperative scheduling."""
    return next(_contact_checkout_cycle)


async def select_contact_dates(page: Page, checkin, max_checkout_attempts: int = 6) -> bool:
    """Fill in check-in/check-out on the contact-host page's own date picker (the
    'Add dates for prices' widget — a separate calendar from the search-page one).
    Rotates through checkout candidates if a given pairing comes back
    'This date is unavailable', instead of giving up on the first miss."""
    opener = page.locator(
        'text=/Add dates for prices/i, input[placeholder="Add date" i], '
        'button:has-text("Check availability")'
    ).first
    if await opener.count() > 0:
        await robust_click(opener, page, attempts=2, clear_overlays=True)
        await random_delay()

    async def click_day(d) -> bool:
        testid = f'calendar-day-{d.strftime("%d/%m/%Y")}'
        cell = page.locator(f'[data-testid="{testid}"]').first
        if await cell.count() == 0:
            return False
        return await robust_click(cell, page, attempts=2, clear_overlays=False)

    if not await click_day(checkin):
        print(f"  Could not select check-in date {checkin}")
        return False
    await random_delay()

    for _ in range(max_checkout_attempts):
        checkout = next_contact_checkout_date()
        if not await click_day(checkout):
            continue
        await random_delay()

        unavailable = page.locator('text=/This date is unavailable/i').first
        if await unavailable.count() > 0 and await unavailable.is_visible():
            print(f"  Checkout {checkout} unavailable, trying next candidate…")
            continue

        save_btn = page.locator('button:has-text("Save")').first
        if await save_btn.count() > 0:
            await robust_click(save_btn, page, attempts=2, clear_overlays=False)
        await random_delay()
        print(f"  Dates set: check-in {checkin} → checkout {checkout}")
        return True

    print(f"  Exhausted {max_checkout_attempts} checkout candidates — proceeding without confirmed dates")
    return False


async def send_message_to_host(page: Page, url: str, contact_url: str = "") -> bool:
    title = ""

    if contact_url:
        # Go straight to the already-scraped contact page
        # (e.g. https://www.airbnb.com/contact_host/<id>/send_message) — this is
        # the same page you land on manually and skips the unreliable step of
        # re-finding/re-clicking a "Contact host" button on the listing page.
        await page.goto(contact_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        await random_delay()
        await close_modals(page)
        # The listing title shows on this page too (used in the message text).
        title = await safe_inner_text(page.locator("h1, h2").first)
    else:
        print(f"  No contactUrl on file for {url} — falling back to click-through")
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        await random_delay()
        await close_modals(page)

        title = await safe_inner_text(page.locator("h1").first)

        contact_btn = page.locator(
            'a[href*="/contact_host/"], a[href*="/send_message"], '
            'button:has-text("Message host"), button:has-text("Contact host")'
        ).first
        if await contact_btn.count() == 0:
            print(f"  Contact button not found on {url}")
            return False
        if not await robust_click(contact_btn, page, timeout=8000):
            print(f"  Could not click contact button on {url}")
            return False
        await random_delay()

    message = build_persuasive_message(title)

    await select_contact_dates(page, CONTACT_CHECKIN_DATE)

    msg_area = page.locator('textarea[placeholder*="message" i], textarea[placeholder*="ask" i], textarea').first
    if await msg_area.count() == 0:
        print(f"  Message textarea not found for {url} (page: {page.url})")
        return False
    try:
        await msg_area.fill(message)
    except Exception as e:
        print(f"  Could not fill message textarea for {url}: {e}")
        return False
    await random_delay()

    send_btn = page.locator('button:has-text("Send message"), button:has-text("Send"), button[type="submit"]').first
    if await send_btn.count() == 0:
        print(f"  Send button not found for {url}")
        return False
    if await send_btn.is_disabled():
        print(f"  Send button is disabled for {url} — dates/fields likely incomplete")
        return False
    try:
        await send_btn.click(timeout=8000)
    except Exception as e:
        print(f"  Send click failed for {url}: {e} — trying JS click")
        if not await js_click(send_btn):
            return False
    await random_delay(2000, 4000)

    success = False
    toast = page.locator('div[role="alert"]:has-text("sent"), div:has-text("Message sent")').first
    if await toast.count() > 0 and await toast.is_visible():
        success = True
    if not success:
        if await send_btn.count() > 0 and await send_btn.is_disabled():
            success = True
        else:
            sent_indicator = page.locator('span:has-text("Sent"), div:has-text("Sent")').first
            if await sent_indicator.count() > 0:
                success = True
    if not success:
        # Last resort: the textarea being gone/cleared after clicking Send is a
        # decent signal the message went through and the UI moved on.
        if await msg_area.count() == 0:
            success = True
    if not success:
        print(f"  No send-confirmation detected for {url} (page: {page.url})")
    return success

def build_persuasive_message(title: str) -> str:
    title = title.strip() or "your place"
    return f"""Hello! 👋

My name is Karl. I came across your listing "{title}" and I'm really interested in reserving it for my friend who will be visiting the area soon.

Before I book, I just wanted to check a few things – like availability for the dates I have in mind and some specific questions about the check‑in process.

Would you mind sharing your WhatsApp number so we can chat more conveniently? I'd really appreciate it.

Looking forward to hearing from you!

Best,
Karl"""

# ---------- SEARCH WORKER ----------
async def search_worker(
    search_queue: asyncio.Queue,
    listing_queue: asyncio.Queue,
    context: BrowserContext,
    max_listings: int,
    days_from_now: int = CHECKIN_DAYS_FROM_NOW,
    nights: int = STAY_NIGHTS,
):
    while True:
        query = await search_queue.get()
        if query is None:
            search_queue.task_done()
            break

        page = await context.new_page()
        try:
            await apply_stealth(page)
            print(f"\n🔍 ===== Starting city: {query} (running concurrently with other cities) =====")
            urls = await perform_search(page, query, max_listings, days_from_now=days_from_now, nights=nights)
            for url in urls:
                await listing_queue.put((url, query))
            print(f"✅ City '{query}' done for this pass — {len(urls)} listings found.\n")
        except Exception as e:
            print(f"❌ Search error for '{query}': {e}")
        finally:
            await page.close()
            search_queue.task_done()
            await random_delay()

async def verify_search_is_zimbabwe(page: Page) -> bool:
    """Read the page's visible text (title + top-of-page heading/breadcrumb area) after
    a search loads and confirm it actually landed in Zimbabwe, not some other country
    Airbnb's autocomplete guessed at (South Africa is the realistic risk given several
    Zimbabwean town names — Norton, Beitbridge — aren't unique). Cheap and conservative:
    if we can't positively find "Zimbabwe" and we DO see a disallowed country hint,
    reject. If neither shows up (ambiguous), we allow it through rather than
    false-positive-rejecting a valid Zimbabwe result whose page text just didn't say
    the country explicitly."""
    try:
        title = (await page.title()) or ""
        heading_text = ""
        heading = page.locator("h1").first
        if await heading.count() > 0:
            heading_text = (await heading.inner_text()) or ""
        combined = f"{title} {heading_text}".lower()
    except Exception:
        return True  # can't verify — don't block a run over a transient read failure

    if any(hint in combined for hint in DISALLOWED_COUNTRY_HINTS):
        return False
    return True


async def click_suggestion_with_retry(page: Page, where_locator, query: str, attempts: int = 3) -> bool:
    """Click a destination suggestion. Unlike robust_click, retries here must NOT press
    Escape — that closes the very dropdown we're trying to click into, which is why this
    used to fail every retry and then fail the forced click too (dropdown item no longer
    in the DOM). Instead, if the dropdown seems to have closed, refocus the Where field
    to reopen it."""
    suggestion = page.locator(f'[role="option"]:has-text("{query}"), li:has-text("{query}")').first
    for _ in range(attempts):
        try:
            await suggestion.wait_for(state="visible", timeout=4000)
            await suggestion.click(timeout=5000)
            return True
        except Exception:
            try:
                await where_locator.click(timeout=4000)
                await sleep_ms(400)
            except Exception:
                pass
    return False


async def select_dates(page: Page, days_from_now: int = CHECKIN_DAYS_FROM_NOW, nights: int = STAY_NIGHTS) -> bool:
    """Click a check-in and check-out day in the "When" calendar that auto-opens after
    picking a destination (see screenshots). Airbnb's calendar day cells use
    data-testid="calendar-day-DD/MM/YYYY" as of this writing; if Airbnb changes that
    markup this just returns False and the caller falls back to searching with no dates,
    rather than breaking the whole search."""
    checkin = datetime.now(timezone.utc).date() + timedelta(days=days_from_now)
    checkout = checkin + timedelta(days=nights)

    async def click_day(d) -> bool:
        testid = f'calendar-day-{d.strftime("%d/%m/%Y")}'
        cell = page.locator(f'[data-testid="{testid}"]').first
        if await cell.count() == 0:
            return False
        return await robust_click(cell, page, attempts=2, clear_overlays=False)

    ok_in = await click_day(checkin)
    await random_delay()
    ok_out = await click_day(checkout) if ok_in else False
    return ok_in and ok_out


def build_search_url(query: str, days_from_now: int = CHECKIN_DAYS_FROM_NOW, nights: int = STAY_NIGHTS) -> str:
    """Deterministic Airbnb search-results URL for a city, dates baked in — used as a
    fallback when the click-through-the-search-box flow doesn't land on a real results
    page (see perform_search)."""
    checkin = datetime.now(timezone.utc).date() + timedelta(days=days_from_now)
    checkout = checkin + timedelta(days=nights)
    place = query.strip().replace(" ", "-")
    return (
        f"https://www.airbnb.com/s/{place}--Zimbabwe/homes"
        f"?checkin={checkin:%Y-%m-%d}&checkout={checkout:%Y-%m-%d}&adults=1"
    )


async def perform_search(
    page: Page,
    query: str,
    max_listings: int,
    days_from_now: int = CHECKIN_DAYS_FROM_NOW,
    nights: int = STAY_NIGHTS,
) -> List[str]:
    await page.goto("https://www.airbnb.com/", wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    await random_delay()
    await dismiss_overlays(page)   # close cookie/region popups before clicking anything

    # Fill search box. This is the click that was failing with:
    #   "<div data-testid="modal-container" ...> ... intercepts pointer events"
    # robust_click clears that overlay and retries instead of failing outright.
    where = page.locator('#bigsearch-query-location-input, input[placeholder*="Search destinations" i]').first
    if not await robust_click(where, page, timeout=10000):
        raise RuntimeError("Could not open the 'Where' search field (blocked by an overlay).")
    await random_delay()
    # Qualify with ", Zimbabwe" — several of our city names (Norton, Beitbridge, etc.)
    # also exist in South Africa/elsewhere, so a bare city name risks autocomplete
    # matching the wrong country. query itself stays unqualified for tagging/CSV.
    qualified_query = f"{query}, Zimbabwe"
    await where.fill(qualified_query)
    await random_delay()

    # Click suggestion (Escape-free retry — see click_suggestion_with_retry docstring).
    # Prefer a suggestion that explicitly says "Zimbabwe"; only fall back to a bare
    # city-name match (and finally Enter) if no Zimbabwe-labeled option shows up.
    picked = await click_suggestion_with_retry(page, where, qualified_query)
    if not picked:
        picked = await click_suggestion_with_retry(page, where, query)
    if not picked:
        await page.keyboard.press("Enter")
    await random_delay()

    # Picking a destination auto-opens the "When" date calendar (see screenshots).
    # Try to actually pick check-in/check-out dates; if that fails for any reason, just
    # dismiss the calendar and search with no dates rather than fail the whole run.
    dates_picked = await select_dates(page, days_from_now=days_from_now, nights=nights)
    if not dates_picked:
        await dismiss_overlays(page)

    # Click search
    search_btn = page.locator('button:has-text("Search")').first
    if await search_btn.count() > 0:
        if not await robust_click(search_btn, page):
            await page.keyboard.press("Enter")
    else:
        await page.keyboard.press("Enter")

    await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
    await random_delay()

    # Guard against the flow silently NOT navigating anywhere: if the destination
    # click / Enter fallback didn't actually submit, we're still sitting on
    # airbnb.com's homepage, which shows a small "homes you might like" recommendation
    # carousel — no pagination, no itemListElement schema. That produces exactly the
    # visible symptom this guard is here to catch: collect_page_links() falling back
    # to a full-page scan, a single un-paginated batch of ~13 listings, and
    # goto_page_number() finding no page-2 link because there IS no results page.
    # A genuine Airbnb search-results page's URL always contains "/s/" and "/homes".
    if "/s/" not in page.url or "/homes" not in page.url:
        direct_url = build_search_url(query, days_from_now=days_from_now, nights=nights)
        print(f"    [{query}] ⚠️ search click didn't land on a real results page "
              f"(landed on: {page.url}) — falling back to a direct search URL: {direct_url}")
        try:
            await page.goto(direct_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            await random_delay()
            await dismiss_overlays(page)
        except Exception as e:
            print(f"    [{query}] ⚠️ direct search URL navigation also failed ({e}) — "
                  f"skipping this city for this pass.")
            return []

    # Hard guarantee: bail out of this city entirely if the search didn't actually land
    # in Zimbabwe (e.g. autocomplete resolved "Norton" to South Africa, or a
    # keyboard-Enter fallback picked Airbnb's own default guess). We'd rather return
    # zero listings for a city than silently scrape South African/other-country stock.
    if not await verify_search_is_zimbabwe(page):
        print(f"    [{query}] ⚠️ search did not land in Zimbabwe (looks like a different "
              f"country) — skipping this city rather than risk scraping non-Zimbabwean listings.")
        return []

    # ---- Exhaust every page of this city's results before returning ----
    # Airbnb search results are numbered-paginated (not infinite scroll). We scroll each
    # page to trigger lazy-loaded listing cards, collect every /rooms/ link on it, then
    # click "Next" and repeat until there's no next page, a page adds nothing new, or we
    # hit MAX_PAGES_PER_CITY as a safety ceiling.
    all_links: Set[str] = set()
    page_num = 1
    started_at = time.monotonic()
    while True:
        await scroll_results(page)
        page_links = await collect_page_links(page)
        new_links = [l for l in page_links if l not in all_links]
        all_links.update(page_links)
        print(f"    [{query}] page {page_num}: {len(page_links)} on page, "
              f"{len(new_links)} new, {len(all_links)} total so far")

        if max_listings > 0 and len(all_links) >= max_listings:
            print(f"    [{query}] reached MAX_LISTINGS_PER_SEARCH cap ({max_listings}).")
            break
        if page_num >= MAX_PAGES_PER_CITY:
            print(f"    [{query}] reached MAX_PAGES_PER_CITY safety limit ({MAX_PAGES_PER_CITY}) — stopping.")
            break
        elapsed = time.monotonic() - started_at
        if elapsed >= MAX_SECONDS_PER_CITY:
            print(f"    [{query}] reached MAX_SECONDS_PER_CITY safety valve "
                  f"({MAX_SECONDS_PER_CITY}s) — cutting this city loose so other "
                  f"cities aren't starved, will pick up more of it next cycle.")
            break
        if not new_links and page_num > 1:
            print(f"    [{query}] page added nothing new — treating results as exhausted.")
            break

        # Walk the numbered pagination bar (1, 2, 3 ... up to whatever's shown, e.g.
        # the "1 ... 12 13 14 15" you see at the bottom of the results) one page at a
        # time, instead of relying solely on the small "Next" arrow.
        advanced = await goto_page_number(page, page_num + 1)
        if not advanced:
            print(f"    [{query}] no further pages — city exhausted after {page_num} page(s).")
            break
        page_num += 1

    links = list(all_links)
    if max_listings > 0:
        links = links[:max_listings]
    return links


async def collect_page_links(page: Page) -> List[str]:
    """Collect /rooms/ links from the ACTUAL search results grid only — not from
    "Recently viewed" or "Based on your <city> search" recommendation carousels that
    Airbnb renders on the same results page. Those carousels pull from your browsing
    history / algorithmic recs and can include listings from any country (Sandton,
    Cape Town, etc. showed up this way even on a correctly-scoped Zimbabwe search) —
    they are NOT filtered by the search query, so grabbing every /rooms/ link on the
    page was letting non-Zimbabwean listings slip in.

    Airbnb marks the real results grid with schema.org ItemList markup
    (itemprop="itemListElement" on each result card / its wrapper); the recommendation
    carousels don't carry that markup. We scope to that first, and only fall back to
    a full-page scan (with a loud warning) if the results container can't be found at
    all, so the scraper never silently returns zero listings due to a markup change."""
    result = await page.evaluate("""() => {
        const scoped = new Set();
        document.querySelectorAll('[itemprop="itemListElement"] a[href*="/rooms/"]').forEach(a => {
            const href = a.getAttribute('href');
            if (!href) return;
            const clean = href.split('?')[0];
            scoped.add(clean.startsWith('http') ? clean : `https://www.airbnb.com${clean}`);
        });
        if (scoped.size > 0) {
            return { links: Array.from(scoped), scoped: true };
        }
        // Fallback: no itemListElement markup found — scan the whole page but flag it,
        // so the caller can warn that results may include off-scope carousel listings.
        const all = new Set();
        document.querySelectorAll('a[href*="/rooms/"]').forEach(a => {
            const href = a.getAttribute('href');
            if (!href) return;
            const clean = href.split('?')[0];
            all.add(clean.startsWith('http') ? clean : `https://www.airbnb.com${clean}`);
        });
        return { links: Array.from(all), scoped: false };
    }""")
    if not result.get("scoped", True):
        print("    ⚠️ collect_page_links: results-grid markup not found — fell back to "
              "a full-page scan, which may include Recently-Viewed/recommendation "
              "carousel listings from OTHER countries. Check output for stray non-ZW listings.")
    return result.get("links", [])


async def goto_page_number(page: Page, target_page_num: int) -> bool:
    """Advance to the numbered page (the '1 ... 12 13 14 15' bar at the bottom
    of the results) by reading the link's real href out of the DOM and
    page.goto()-ing it directly — NOT by clicking.

    Clicking was the bug: Playwright's click requires the element to be
    "actionable" (in viewport, not covered by another element, stable, etc.),
    and Airbnb's pagination links routinely fail one of those checks — off
    the bottom of a tall page, briefly covered while the grid re-renders,
    etc. When the click silently fails, goto_page_number/go_to_next_page
    returned False and the whole city got marked "exhausted" after page 1,
    even though the href was sitting right there in the DOM the whole time.
    Reading href and navigating to it has none of those failure modes.
    """
    href = await page.evaluate(
        """(targetPage) => {
            // Most specific: an <a> whose aria-label is exactly "Page N".
            let link = document.querySelector(`a[aria-label="Page ${targetPage}"]`);
            if (link && link.href) return link.href;

            // Fallback: any <a> inside a pagination-labeled <nav> whose visible
            // text is exactly the target page number (covers builds that don't
            // set aria-label="Page N" on each link).
            const navs = document.querySelectorAll('nav');
            for (const nav of navs) {
                const label = (nav.getAttribute('aria-label') || '').toLowerCase();
                if (!label.includes('pagination')) continue;
                const links = nav.querySelectorAll('a[href]');
                for (const a of links) {
                    if (a.textContent.trim() === String(targetPage)) return a.href;
                }
            }
            return null;
        }""",
        target_page_num,
    )

    if href:
        try:
            await page.goto(href, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            await random_delay()
            return True
        except Exception as e:
            print(f"    ⚠️ goto_page_number: found href for page {target_page_num} but "
                  f"navigation failed ({e}) — treating as no further pages.")
            return False

    # No numbered link found in the DOM for this page number at all. Before
    # concluding we're done, try the "Next" arrow's href the same way (some
    # short result sets only render Prev/Next, no numbered bar).
    next_href = await page.evaluate(
        """() => {
            const btn = document.querySelector('a[aria-label="Next"]');
            if (!btn) return null;
            if (btn.getAttribute('aria-disabled') === 'true') return null;
            return btn.href || null;
        }"""
    )
    if next_href:
        try:
            await page.goto(next_href, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            await random_delay()
            return True
        except Exception:
            pass

    # Last resort: the old click-based path, in case href extraction ever
    # misses a markup variant it doesn't know about yet.
    return await go_to_next_page(page)


async def go_to_next_page(page: Page) -> bool:
    """Click Airbnb's search-results pagination 'Next' arrow. Returns False when
    there's no next page (last page reached) so perform_search knows to stop and
    treat the city as fully exhausted. Kept as the fallback goto_page_number()
    reaches for when a numbered page link isn't present."""
    next_btn = page.locator(
        'a[aria-label="Next"], nav[aria-label*="Search results pagination" i] a[aria-label="Next"]'
    ).first
    if await next_btn.count() == 0:
        return False
    try:
        aria_disabled = await next_btn.get_attribute("aria-disabled")
        if aria_disabled == "true":
            return False
        await next_btn.scroll_into_view_if_needed(timeout=5000)
        clicked = await robust_click(next_btn, page, timeout=8000, attempts=2)
        if not clicked:
            return False
        await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
        await random_delay()
        return True
    except Exception:
        return False

async def scroll_results(page: Page) -> None:
    vs = page.viewport_size  # property, not a coroutine — calling it as vs() is what raised
    viewport_h = vs["height"] if vs else 900
    scroll_h = await page.evaluate("document.body.scrollHeight")
    pos = 0
    for _ in range(5):
        while pos < scroll_h:
            pos = min(pos + int(viewport_h * 0.8), scroll_h)
            await page.evaluate(f"window.scrollTo(0, {pos})")
            await sleep_ms(550)
        new_h = await page.evaluate("document.body.scrollHeight")
        if new_h == scroll_h:
            break
        scroll_h = new_h
    await page.evaluate("window.scrollTo(0, 0)")
    await sleep_ms(400)

# ---------- MASTER INDEX & CSV ----------
async def index_worker(result_queue: asyncio.Queue):
    master_index = []
    if MASTER_INDEX_FILE.exists():
        try:
            master_index = json.loads(MASTER_INDEX_FILE.read_text(encoding="utf-8"))
        except:
            pass
    seen_urls = {item["url"] for item in master_index}

    csv_headers = [
        "listingId", "title", "searchCity", "location", "lat", "lng", "url", "datedUrl",
        "price", "priceUsdPerNight", "priceCurrency", "rating",
        "reviewsCount", "host", "superhost", "contactUrl", "amenities",
        "descriptionSnippet", "folder"
    ]
    if not CSV_FILE.exists():
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=csv_headers)
            writer.writeheader()

    while True:
        item = await result_queue.get()
        if item is None:
            result_queue.task_done()
            break
        data, folder_path = item
        price_detail = data.get("priceDetail") or {}
        entry = {
            "listingId": data["listingId"],
            "title": data["title"],
            "searchCity": data.get("searchCity", ""),
            "location": data["location"],
            "lat": data.get("lat"),
            "lng": data.get("lng"),
            "url": data["url"],
            "datedUrl": data.get("datedUrl", ""),
            "price": data["price"],
            "priceUsdPerNight": price_detail.get("usd_per_night"),
            "priceCurrency": price_detail.get("currency"),
            "rating": data["rating"],
            "reviewsCount": data["reviewsCount"],
            "host": data["host"]["name"],
            "superhost": data["host"]["superhost"],
            "contactUrl": data["contactUrl"],
            "amenities": " | ".join(data["amenities"]),
            "descriptionSnippet": data["description"][:200],
            "folder": str(folder_path),
        }
        if data["url"] in seen_urls:
            for i, old in enumerate(master_index):
                if old["url"] == data["url"]:
                    master_index[i] = entry
                    break
        else:
            master_index.append(entry)
            seen_urls.add(data["url"])
            with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=csv_headers)
                writer.writerow(entry)

        MASTER_INDEX_FILE.write_text(json.dumps(master_index, indent=2), encoding="utf-8")
        # Keep a standalone coordinates.json in sync too, same shape backfill_coordinates.py
        # produces, so any downstream tooling built around that file keeps working.
        try:
            coords_map = {}
            if COORDS_FILE.exists():
                coords_map = json.loads(COORDS_FILE.read_text(encoding="utf-8"))
            coords_map[data["listingId"]] = {
                "lat": data.get("lat"), "lng": data.get("lng"), "location": data["location"],
            }
            COORDS_FILE.write_text(json.dumps(coords_map, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

        # Push straight through to the Karlon server too — this is what
        # makes the freshly-scraped listing show up (with photos + USD
        # price) in the app's house-icon popup in near-real time. Use the
        # dates this particular listing was actually priced for (set on
        # scrape_listing's return value) rather than assuming the global
        # fixed window — matters once on-demand jobs (different dates per
        # request) share this same index_worker/result_queue pipeline.
        req_days = data.get("requestedDaysFromNow", CHECKIN_DAYS_FROM_NOW)
        req_nights = data.get("requestedNights", STAY_NIGHTS)
        req_checkin = datetime.now(timezone.utc).date() + timedelta(days=req_days)
        req_checkout = req_checkin + timedelta(days=req_nights)
        await push_listing_to_karlon(data, price_detail, checkin_date=req_checkin, checkout_date=req_checkout)

        result_queue.task_done()

async def run_ondemand_job(
    context: BrowserContext,
    checkpoint: Checkpoint,
    location: str,
    check_in: date,
    check_out: date,
    job_id: Optional[int] = None,
) -> int:
    """Small, fast, single-city scrape for the EXACT dates a guest picked in the
    app (see GET /api/houses/available?check_in=&check_out= and the audit's P1
    finding — the full sweep only ever prices CHECKIN_DAYS_FROM_NOW/STAY_NIGHTS,
    so any other date pair returns nothing). Pushes results straight to Karlon
    with those exact dates via push_listing_to_karlon and returns how many
    listings were pushed.

    Deliberately does NOT touch checkpoint.scraped_urls or the master
    index/CSV — those belong to the full sweep. An on-demand job re-prices even
    a URL scraped many times before, because what changed is the dates, not
    the listing, so the usual "already scraped, skip" dedup would be wrong here."""
    today = datetime.now(timezone.utc).date()
    days_from_now = (check_in - today).days
    nights = (check_out - check_in).days
    if days_from_now < 0 or nights <= 0:
        print(f"[ondemand] ⚠️ rejecting job for {location} {check_in}→{check_out}: "
              f"check-in must be today or later and check-out must be after check-in.")
        return 0

    print(f"[ondemand] 🔎 live re-price: {location}, {check_in} → {check_out} "
          f"({nights} night(s), {days_from_now} day(s) out)")

    image_sem = asyncio.Semaphore(MAX_IMAGE_DOWNLOADS)
    search_page = await context.new_page()
    urls: List[str] = []
    try:
        await apply_stealth(search_page)
        urls = await perform_search(
            search_page, location, ONDEMAND_MAX_LISTINGS_PER_JOB,
            days_from_now=days_from_now, nights=nights,
        )
    except Exception as e:
        print(f"[ondemand] ❌ search failed for {location}: {e}")
        return 0
    finally:
        await search_page.close()

    pushed = 0
    for url in urls[:ONDEMAND_MAX_LISTINGS_PER_JOB]:
        page = await context.new_page()
        try:
            await apply_stealth(page)
            data = await scrape_listing(
                page, url, image_sem, city_hint=location,
                days_from_now=days_from_now, nights=nights,
            )
            if not data:
                continue
            location_text = str(data.get("location") or "").lower()
            if any(hint in location_text for hint in DISALLOWED_COUNTRY_HINTS):
                print(f"[ondemand] ⛔ skipping {url} — non-Zimbabwean location.")
                continue
            price_detail = data.get("priceDetail") or {}
            await push_listing_to_karlon(
                data, price_detail, checkin_date=check_in, checkout_date=check_out,
                refresh_job_id=job_id,
            )
            pushed += 1
        except Exception as e:
            print(f"[ondemand] ❌ error scraping {url}: {e}")
        finally:
            await page.close()
            await random_delay()

    print(f"[ondemand] ✅ done: {location} {check_in}→{check_out} — {pushed}/{len(urls)} listing(s) pushed")
    return pushed


async def ondemand_poll_loop(context: BrowserContext, checkpoint: Checkpoint) -> None:
    """Runs for the lifetime of the process, independent of the full-sweep cycle
    loop in main() below — so a guest picking new dates in the app doesn't have
    to wait for the current 20-city sweep to finish before getting a price.
    Expects two new server endpoints (server-side, not built here — see the
    ENABLE_ONDEMAND_REFRESH comment near the top of this file for the contract):
      GET  {KARLON_SERVER_URL}/api/houses/refresh/pending
           -> [{"id": <job id>, "location": str, "check_in": "YYYY-MM-DD",
                "check_out": "YYYY-MM-DD"}, ...]
      POST {KARLON_SERVER_URL}/api/houses/refresh/{id}/complete
           body: {"pushed": <int>}
    A 404 (endpoint doesn't exist yet) or any connection error just logs once
    and keeps retrying every ONDEMAND_POLL_INTERVAL_SECONDS — this must never
    crash or stall the scraper, and the full sweep is completely unaffected
    either way."""
    if not ENABLE_ONDEMAND_REFRESH:
        return
    import aiohttp
    session = await _karlon_http()
    pending_url = f"{KARLON_SERVER_URL}{ONDEMAND_REFRESH_PATH_PREFIX}/pending"
    warned_missing_endpoint = False
    while True:
        jobs = []
        try:
            async with session.get(pending_url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status == 404:
                    if not warned_missing_endpoint:
                        print(f"[ondemand] ℹ️ {pending_url} not found (server-side job "
                              f"endpoint not built yet) — on-demand refresh stays idle "
                              f"until it exists. Full sweep is unaffected.")
                        warned_missing_endpoint = True
                elif resp.status == 200:
                    jobs = await resp.json()
                    warned_missing_endpoint = False
                else:
                    print(f"[ondemand] ⚠️ {pending_url} returned {resp.status}")
        except Exception as e:
            print(f"[ondemand] ⚠️ couldn't poll {pending_url}: {e}")

        for job in jobs or []:
            try:
                job_id = job["id"]
                location = job["location"]
                check_in = datetime.strptime(job["check_in"], "%Y-%m-%d").date()
                check_out = datetime.strptime(job["check_out"], "%Y-%m-%d").date()
            except Exception as e:
                print(f"[ondemand] ⚠️ skipping malformed job {job}: {e}")
                continue

            pushed = await run_ondemand_job(context, checkpoint, location, check_in, check_out, job_id=job_id)
            try:
                await session.post(
                    f"{KARLON_SERVER_URL}{ONDEMAND_REFRESH_PATH_PREFIX}/{job_id}/complete",
                    json={"pushed": pushed},
                    timeout=aiohttp.ClientTimeout(total=60),
                )
            except Exception as e:
                print(f"[ondemand] ⚠️ couldn't ack job {job_id} complete: {e}")

        await asyncio.sleep(ONDEMAND_POLL_INTERVAL_SECONDS)


# ---------- MAIN ----------
async def main():
    global CURRENT_ZAR_TO_USD_RATE, SEARCH_QUERIES
    from local_config import print_active_config
    print_active_config()
    print(f"[airbnb_scraper] will push ingested listings to {KARLON_SERVER_URL}/api/houses/ingest "
          f"(login timeout {LOGIN_TIMEOUT_SECONDS}s)")
    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    checkpoint = Checkpoint.load()
    print(f"Resuming: {len(checkpoint.scraped_urls)} scraped, {len(checkpoint.sent_urls)} messaged.")

    SEARCH_QUERIES = prompt_for_search_queries(SEARCH_QUERIES)

    CURRENT_ZAR_TO_USD_RATE = await fetch_zar_to_usd_rate()
    print(f"Cities queued ({MAX_SEARCH_WORKERS} run concurrently, each capped at "
          f"{MAX_SECONDS_PER_CITY}s so none can starve the others): "
          f"{', '.join(SEARCH_QUERIES)}\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)

        # Try the saved profile first; only fall back to the manual login flow if it's
        # missing or expired. The rest of the pipeline never starts without a working,
        # logged-in context — no more silent "proceeding without auth".
        context = await try_restore_session(browser)
        if context is None:
            context = await login_with_timeout(browser)

        if context is None:
            print("\n❌ Could not establish a logged-in Airbnb session. Aborting — nothing will be scraped or messaged.")
            print("   Re-run the script and complete the login within the time limit.")
            await browser.close()
            return

        ondemand_task = asyncio.create_task(ondemand_poll_loop(context, checkpoint))

        cycle_num = 0
        try:
            while True:
                cycle_num += 1
                print(f"\n{'='*60}\n🔁 Starting cycle {cycle_num} — re-scanning all {len(SEARCH_QUERIES)} "
                      f"Zimbabwe cities (Ctrl+C to stop)\n{'='*60}")

                search_queue = asyncio.Queue()
                listing_queue = asyncio.Queue()
                msg_queue = asyncio.Queue()
                result_queue = asyncio.Queue()

                cycle_cities = list(SEARCH_QUERIES)
                random.shuffle(cycle_cities)   # rotate priority each cycle — no city is always first/last
                for q in cycle_cities:
                    await search_queue.put(q)
                for url in LISTING_URLS:
                    await listing_queue.put((url, ""))  # manually-provided URL, no city tag

                image_sem = asyncio.Semaphore(MAX_IMAGE_DOWNLOADS)

                # ---- PHASE 1: SEARCH / HARVEST ----
                # Walk every city's search results end-to-end — every page, 1 through
                # however many exist (the "1 ... 12 13 14 15" bar) — collecting every
                # listing link before touching a single listing page. scrape_workers are
                # deliberately NOT started yet: starting them alongside search_workers
                # let scraping begin on early links while other pages/cities were still
                # being harvested. Splitting into two clean phases guarantees the full
                # link set is in hand before phase 2 opens a single listing.
                print(f"\n🔎 Phase 1/2: searching — harvesting every listing link across "
                      f"all result pages for {', '.join(SEARCH_QUERIES)}...")

                search_workers = []
                for _ in range(MAX_SEARCH_WORKERS):
                    w = asyncio.create_task(search_worker(search_queue, listing_queue, context, MAX_LISTINGS_PER_SEARCH))
                    search_workers.append(w)

                await search_queue.join()
                for _ in search_workers:
                    await search_queue.put(None)
                await asyncio.gather(*search_workers)

                harvested = listing_queue.qsize()
                print(f"✅ Phase 1/2 complete — {harvested} listing link(s) harvested "
                      f"from every page. Starting phase 2 (individual scraping) now.\n")

                # ---- PHASE 2: SCRAPE ----
                # Only now do we start opening individual listing pages for images/info.
                scrape_workers = []
                for _ in range(MAX_SCRAPE_WORKERS):
                    w = asyncio.create_task(scrape_worker(listing_queue, result_queue, context, checkpoint, image_sem))
                    scrape_workers.append(w)

                msg_workers = []
                if ENABLE_MESSAGING:
                    for _ in range(MAX_MESSAGE_WORKERS):
                        w = asyncio.create_task(message_worker(msg_queue, context, checkpoint))
                        msg_workers.append(w)

                index_worker_task = asyncio.create_task(index_worker(result_queue))

                await listing_queue.join()
                for _ in scrape_workers:
                    await listing_queue.put(None)
                await asyncio.gather(*scrape_workers)

                if ENABLE_MESSAGING:
                    print("\nScraping complete for this cycle. Now sending messages to hosts...")
                    master = []
                    if MASTER_INDEX_FILE.exists():
                        master = json.loads(MASTER_INDEX_FILE.read_text(encoding="utf-8"))
                    for item in master:
                        url = item["url"]
                        if url not in checkpoint.sent_urls:
                            await msg_queue.put((url, item.get("contactUrl", "")))

                    await msg_queue.join()
                    for _ in msg_workers:
                        await msg_queue.put(None)
                    await asyncio.gather(*msg_workers)
                else:
                    print("\nScraping complete for this cycle. Messaging is paused (ENABLE_MESSAGING = False).")

                await result_queue.put(None)
                await index_worker_task

                print(f"\n✅ Cycle {cycle_num} completed.")
                print(f"Master index: {MASTER_INDEX_FILE}")
                print(f"CSV: {CSV_FILE}")

                final_master = []
                if MASTER_INDEX_FILE.exists():
                    final_master = json.loads(MASTER_INDEX_FILE.read_text(encoding="utf-8"))
                by_city: Dict[str, int] = {}
                for item in final_master:
                    city = item.get("searchCity") or "(direct URL)"
                    by_city[city] = by_city.get(city, 0) + 1
                print(f"\nTotals: {len(final_master)} listings")
                print("By city:")
                for city, count in sorted(by_city.items(), key=lambda kv: -kv[1]):
                    print(f"  {city}: {count}")

                CURRENT_ZAR_TO_USD_RATE = await fetch_zar_to_usd_rate()

                print(f"\n💤 Sleeping {CYCLE_REST_SECONDS}s before the next full re-scan "
                      f"(cycle {cycle_num + 1})...")
                await asyncio.sleep(CYCLE_REST_SECONDS)
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\n🛑 Stopped by user. Progress up to the last completed listing is already saved.")
        finally:
            ondemand_task.cancel()
            try:
                await ondemand_task
            except asyncio.CancelledError:
                pass
            if _karlon_session is not None and not _karlon_session.closed:
                await _karlon_session.close()
            await browser.close()

if __name__ == "__main__":
    asyncio.run(main())