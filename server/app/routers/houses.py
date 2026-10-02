"""routers/houses.py — bridges the Airbnb scraper (airbnb_parallel_system.py)
and the app's "available houses" screen.

Data model
==========
house_listings   one row per *property* (keyed by URL): title, photos,
                 coords, and the most recent price seen. Upserted on every
                 ingest — this is the identity record.
listing_offers   one row per (property, check_in, check_out) the scraper
                 actually priced. Never overwritten by a scrape for other
                 dates, so GET /available?location=&check_in=&check_out=
                 keeps returning exactly what was harvested for that window
                 until check_out is in the past (purged lazily on ingest).
static/houses/   server-hosted copies of each listing's first N photos,
                 fetched in a background task right after ingest, so the
                 photos sent to a guest come from *this* server, not from a
                 signed Airbnb CDN URL that may expire or that wa_bridge
                 can't turn into a valid Windows temp filename.

Flow
====
1. Scraper POSTs each listing to POST /api/houses/ingest the moment it's
   scraped (progressively — one listing per POST is fine, that is what makes
   the app's live search fill in as the scrape runs).
2. App polls GET /api/houses/available?location=&check_in=&check_out=&limit=
   every few seconds while the search screen is open.
3. Tapping Send calls POST /api/houses/send — queues the listing's photos
   (server-hosted when cached) + a title/price/dates caption as 'out'
   messages on the normal outbox → wa_bridge → WhatsApp path. The Airbnb
   URL is deliberately NOT included in the caption.
"""

import hashlib
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests
from fastapi import APIRouter, BackgroundTasks, HTTPException

from ..config import (
    HOUSE_IMAGE_FETCH_TIMEOUT_SEC,
    HOUSE_IMAGES_TO_CACHE,
    HOUSES_MEDIA_DIR,
)
from ..database import get_db
from ..models import HouseIngestIn, HouseSendIn, serialize_message
from ..ws_manager import manager

router = APIRouter(prefix="/api/houses", tags=["houses"])

MAX_AVAILABLE_LIMIT = 40


# ─────────────────────────────── helpers ──────────────────────────────────────

def listing_key(url: str) -> str:
    """Filesystem-safe, stable key for one property (used for the photo dir)."""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def offer_id_for(url: str, check_in: str, check_out: str) -> str:
    return hashlib.sha1(f"{url}|{check_in}|{check_out}".encode("utf-8")).hexdigest()[:20]


def _norm_location(loc: str) -> str:
    return (loc or "").strip().lower()


def _json_list(raw) -> list:
    try:
        v = json.loads(raw or "[]")
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _existing_local_images(paths: list[str]) -> list[str]:
    """Only local photo paths whose file is really on disk (a Space rebuild
    wipes static/houses/ — the row would still list them)."""
    out = []
    for p in paths:
        rel = p.split("/static/houses/", 1)[-1] if "/static/houses/" in p else None
        if rel and (HOUSES_MEDIA_DIR / rel).exists():
            out.append(p)
    return out


def _row_to_listing(row, offer=None) -> dict:
    """Wire shape for the app (HouseListingDto). `images` is what the app
    should display/send: server-hosted copies when they exist, else the
    original scraped URLs. `images_remote` always carries the originals."""
    d = dict(row)
    remote = _json_list(d.get("images"))
    local = _existing_local_images(_json_list(d.get("images_local")))
    d["images_remote"] = remote
    d["images"] = local if local else remote
    d["images_local"] = local
    d["listing_key"] = listing_key(d["url"])
    if offer is not None:
        o = dict(offer)
        d["offer_id"] = o["id"]
        d["check_in"] = o["check_in"]
        d["check_out"] = o["check_out"]
        d["nights"] = o.get("nights")
        d["price_raw"] = o.get("price_raw")
        d["price_currency"] = o.get("price_currency")
        d["price_zar_per_night"] = o.get("price_zar_per_night")
        d["price_usd_per_night"] = o.get("price_usd_per_night")
        d["fx_rate_zar_per_usd"] = o.get("fx_rate_zar_per_usd")
        d["scraped_at"] = o.get("scraped_at")
    else:
        ci, co = d.get("check_in"), d.get("check_out")
        d["offer_id"] = offer_id_for(d["url"], ci, co) if ci and co else None
        d["scraped_at"] = d.get("updated_at")
    return d


def _cache_listing_images(url: str, image_urls: list[str]) -> None:
    """Background task: mirror the first HOUSE_IMAGES_TO_CACHE photos of one
    listing into static/houses/<key>/ and record their /static paths on the
    row. Best-effort — a failed download just leaves the remote URL in use."""
    key = listing_key(url)
    folder = HOUSES_MEDIA_DIR / key
    folder.mkdir(parents=True, exist_ok=True)
    local_paths: list[str] = []
    for i, img in enumerate(image_urls[:HOUSE_IMAGES_TO_CACHE]):
        target = folder / f"{i + 1}.jpg"
        if target.exists() and target.stat().st_size > 0:
            local_paths.append(f"/static/houses/{key}/{target.name}")
            continue
        try:
            r = requests.get(
                img,
                timeout=HOUSE_IMAGE_FETCH_TIMEOUT_SEC,
                headers={"User-Agent": "Mozilla/5.0 (KarlonImageCache)"},
            )
            r.raise_for_status()
            if not r.content or "image" not in r.headers.get("content-type", "image"):
                continue
            target.write_bytes(r.content)
            local_paths.append(f"/static/houses/{key}/{target.name}")
        except Exception as e:
            print(f"[houses] image cache failed for {img[:80]}...: {e}")
    if not local_paths:
        return
    conn = get_db()
    try:
        conn.execute(
            "UPDATE house_listings SET images_local=? WHERE id=?",
            (json.dumps(local_paths), url),
        )
        conn.commit()
    finally:
        conn.close()


def _purge_expired_offers(conn) -> None:
    """An offer is only meaningful until its check-out date has passed."""
    today = date.today().isoformat()
    conn.execute("DELETE FROM listing_offers WHERE check_out < ?", (today,))


def _split_zar_usd(item) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Fill in whichever of (zar, usd, fx) the scraper didn't send, when the
    other two are known. Returns (zar_per_night, usd_per_night, fx_rate)."""
    zar, usd, fx = item.price_zar_per_night, item.price_usd_per_night, item.fx_rate_zar_per_usd
    if zar is None and usd and fx:
        zar = round(usd * fx, 2)
    if usd is None and zar and fx:
        usd = round(zar / fx, 2)
    if fx is None and zar and usd:
        fx = round(zar / usd, 4)
    return zar, usd, fx


# ─────────────────────────────── ingest ───────────────────────────────────────

@router.post("/ingest")
def ingest_listings(body: HouseIngestIn, background: BackgroundTasks):
    """Upsert scraped listings (property row) and record the price offer for
    the exact (check_in, check_out) each one was scraped for. Called by the
    scraper, not the app. Photos are mirrored locally in the background."""
    if not body.listings:
        return {"ingested": 0, "offers": 0}

    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    offers = 0
    for item in body.listings:
        loc = _norm_location(item.location)
        zar, usd, fx = _split_zar_usd(item)
        nights = item.nights
        if nights is None and item.check_in and item.check_out:
            try:
                nights = (date.fromisoformat(item.check_out) - date.fromisoformat(item.check_in)).days
            except ValueError:
                nights = None

        conn.execute(
            """
            INSERT INTO house_listings
                (id, url, listing_id, title, location, check_in, check_out, price_raw,
                 price_currency, price_zar_per_night, price_usd_per_night, fx_rate_zar_per_usd,
                 nights, images, lat, lng, updated_at, first_seen_at,
                 neighbourhood, capacity, rating, reviews_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                listing_id=COALESCE(excluded.listing_id, house_listings.listing_id),
                title=CASE WHEN excluded.title != '' THEN excluded.title ELSE house_listings.title END,
                location=excluded.location,
                check_in=excluded.check_in,
                check_out=excluded.check_out,
                price_raw=excluded.price_raw,
                price_currency=excluded.price_currency,
                price_zar_per_night=excluded.price_zar_per_night,
                price_usd_per_night=excluded.price_usd_per_night,
                fx_rate_zar_per_usd=excluded.fx_rate_zar_per_usd,
                nights=excluded.nights,
                images=CASE WHEN excluded.images != '[]' THEN excluded.images ELSE house_listings.images END,
                lat=COALESCE(excluded.lat, house_listings.lat),
                lng=COALESCE(excluded.lng, house_listings.lng),
                updated_at=excluded.updated_at,
                first_seen_at=COALESCE(house_listings.first_seen_at, excluded.first_seen_at),
                neighbourhood=COALESCE(NULLIF(excluded.neighbourhood, ''), house_listings.neighbourhood),
                capacity=COALESCE(NULLIF(excluded.capacity, ''), house_listings.capacity),
                rating=COALESCE(NULLIF(excluded.rating, ''), house_listings.rating),
                reviews_count=COALESCE(NULLIF(excluded.reviews_count, ''), house_listings.reviews_count)
            """,
            (
                item.url, item.url, item.listing_id, item.title or "", loc,
                item.check_in, item.check_out, item.price_raw,
                item.price_currency, zar, usd, fx, nights,
                json.dumps(item.images), item.lat, item.lng, now, now,
                item.neighbourhood, item.capacity, item.rating, item.reviews_count,
            ),
        )

        if item.check_in and item.check_out:
            conn.execute(
                """
                INSERT INTO listing_offers
                    (id, listing_url, location, check_in, check_out, nights, price_raw,
                     price_currency, price_zar_per_night, price_usd_per_night,
                     fx_rate_zar_per_usd, refresh_job_id, scraped_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    location=excluded.location,
                    nights=excluded.nights,
                    price_raw=excluded.price_raw,
                    price_currency=excluded.price_currency,
                    price_zar_per_night=excluded.price_zar_per_night,
                    price_usd_per_night=excluded.price_usd_per_night,
                    fx_rate_zar_per_usd=excluded.fx_rate_zar_per_usd,
                    refresh_job_id=COALESCE(excluded.refresh_job_id, listing_offers.refresh_job_id),
                    scraped_at=excluded.scraped_at
                """,
                (
                    offer_id_for(item.url, item.check_in, item.check_out), item.url, loc,
                    item.check_in, item.check_out, nights, item.price_raw,
                    item.price_currency, zar, usd, fx, item.refresh_job_id, now,
                ),
            )
            offers += 1
            if item.refresh_job_id is not None:
                # Live progress counter the app can show ("3 found so far")
                conn.execute(
                    "UPDATE refresh_jobs SET pushed_count = COALESCE(pushed_count, 0) + 1 "
                    "WHERE id=? AND status='in_progress'",
                    (item.refresh_job_id,),
                )

        if item.images:
            background.add_task(_cache_listing_images, item.url, list(item.images))

    _purge_expired_offers(conn)
    conn.commit()
    conn.close()
    return {"ingested": len(body.listings), "offers": offers}


# ─────────────────────────────── queries ──────────────────────────────────────

@router.get("/locations")
def known_locations():
    """Distinct locations currently in the table — for the popup's location picker."""
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT location FROM house_listings ORDER BY location ASC"
    ).fetchall()
    conn.close()
    return [r["location"] for r in rows]


@router.get("/available")
def available_listings(
    location: str,
    check_in: Optional[str] = None,
    check_out: Optional[str] = None,
    limit: int = 6,
):
    """Listings for a location, newest-scraped first.

    With check_in+check_out: exactly the offers scraped for that window (from
    listing_offers, so a later scrape for other dates doesn't hide them).
    Without dates: the freshest property rows for the city regardless of
    dates. The app polls this while its search screen is open, so new rows
    show up progressively as the scraper pushes them.
    """
    limit = max(1, min(limit, MAX_AVAILABLE_LIMIT))
    loc = _norm_location(location)
    conn = get_db()
    if check_in and check_out:
        rows = conn.execute(
            """
            SELECT o.id AS _oid, h.*
            FROM listing_offers o
            JOIN house_listings h ON h.id = o.listing_url
            WHERE o.location = ? AND o.check_in = ? AND o.check_out = ?
            ORDER BY o.scraped_at DESC LIMIT ?
            """,
            (loc, check_in, check_out, limit),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            oid = d.pop("_oid")
            offer = conn.execute("SELECT * FROM listing_offers WHERE id=?", (oid,)).fetchone()
            out.append(_row_to_listing(d, offer))
        conn.close()
        return out

    rows = conn.execute(
        "SELECT * FROM house_listings WHERE location = ? ORDER BY updated_at DESC LIMIT ?",
        (loc, limit),
    ).fetchall()
    conn.close()
    return [_row_to_listing(r) for r in rows]


@router.get("/listing")
def get_listing(url: str, offer_id: Optional[str] = None):
    """One property by URL (+ optionally the specific offer to price it with).
    Used by the invoice form to re-hydrate a listing picked earlier."""
    conn = get_db()
    row = conn.execute("SELECT * FROM house_listings WHERE id=?", (url,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "listing not found")
    offer = None
    if offer_id:
        offer = conn.execute("SELECT * FROM listing_offers WHERE id=?", (offer_id,)).fetchone()
    conn.close()
    return _row_to_listing(row, offer)


@router.get("/offers")
def offers_for_listing(url: str):
    """Every date window this property has been priced for (still valid)."""
    conn = get_db()
    _purge_expired_offers(conn)
    conn.commit()
    rows = conn.execute(
        "SELECT * FROM listing_offers WHERE listing_url=? ORDER BY check_in ASC", (url,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─────────────────────────────── send to WhatsApp ─────────────────────────────

def _fmt_date(s: Optional[str]) -> str:
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%d %b %Y")
    except Exception:
        return s or ""


def build_listing_message(listing: dict, option: int = 1, of: int = 1) -> str:
    """The text sent BEFORE a listing's photos: what was found, where, how big,
    which dates it is free for, and the price per night and in total.

    Per night is the stay total divided by the nights (the scraper now stores
    that correctly — it used to store the whole-stay total as the nightly
    rate). No "Managed by …" footer, no Airbnb link."""
    lines = []
    if option == 1:
        lines.append("This is what I have found for you:")
        lines.append("")
    if of > 1:
        lines.append(f"*Option {option} of {of}*")
    lines.append(f"🏠 {listing.get('title') or 'Available property'}")

    place = str(listing.get("location") or "").strip().title()
    hood = str(listing.get("neighbourhood") or "").strip()
    if hood and hood.lower() not in place.lower():
        place = f"{hood}, {place}" if place else hood
    if place:
        lines.append(f"📍 {place}")
    if listing.get("capacity"):
        lines.append(f"👥 {listing['capacity']}")
    if listing.get("rating"):
        rc = listing.get("reviews_count")
        lines.append(f"⭐ {listing['rating']}" + (f" ({rc} review{'s' if str(rc) != '1' else ''})" if rc else ""))

    nights = listing.get("nights")
    if listing.get("check_in") and listing.get("check_out"):
        stay = f"📅 Free {_fmt_date(listing['check_in'])} → {_fmt_date(listing['check_out'])}"
        if nights:
            stay += f" ({nights} night{'s' if nights != 1 else ''})"
        lines.append(stay)

    usd = listing.get("price_usd_per_night")
    if usd:
        price = f"💵 USD {usd:,.2f} per night"
        if nights:
            price += f" · USD {usd * nights:,.2f} total"
        lines.append(price)
    elif listing.get("price_raw"):
        lines.append(f"💵 {listing['price_raw']}")
    else:
        lines.append("💵 Price on request")
    return "\n".join(lines)


# Kept for callers/tests that used the old name: same text, no footer.
build_listing_caption = build_listing_message


@router.post("/send")
async def send_listings(body: HouseSendIn):
    """Queue, per chosen listing, a details message (see build_listing_message)
    followed by its photos, as outbound WhatsApp messages for one chat.
    Photos are the server-hosted copies when cached (fall back to the scraped
    URLs) and go without captions; the Airbnb link is intentionally not sent."""
    conn = get_db()
    if not conn.execute("SELECT 1 FROM chats WHERE id=?", (body.chat_id,)).fetchone():
        conn.close()
        raise HTTPException(404, "chat not found")
    if not body.listing_ids:
        conn.close()
        raise HTTPException(400, "no listings selected")

    now = datetime.now(timezone.utc)
    max_imgs = max(1, min(body.max_images_per_listing, 10))
    queued_ids: list[str] = []

    for n, listing_id in enumerate(body.listing_ids):
        row = conn.execute("SELECT * FROM house_listings WHERE id=?", (listing_id,)).fetchone()
        if not row:
            continue
        offer = None
        if body.offer_ids and n < len(body.offer_ids) and body.offer_ids[n]:
            offer = conn.execute("SELECT * FROM listing_offers WHERE id=?", (body.offer_ids[n],)).fetchone()
        listing = _row_to_listing(row, offer)
        details = build_listing_message(listing, option=n + 1, of=len(body.listing_ids))

        # Details text first, then the photos (no captions). created_at is
        # nudged a few ms per message so the outbox (ORDER BY created_at)
        # delivers them in exactly this order.
        msg_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO messages "
            "(id, chat_id, sender, kind, text, poll_id, created_at, media_url, media_type, "
            "direction, wa_status) "
            "VALUES (?, ?, ?, 'text', ?, NULL, ?, NULL, NULL, 'out', 'pending')",
            (msg_id, body.chat_id, body.sender, details, now.isoformat()),
        )
        queued_ids.append(msg_id)

        for i, img_url in enumerate(listing["images"][:max_imgs]):
            msg_id = str(uuid.uuid4())
            ts = (now + timedelta(milliseconds=i + 1)).isoformat()
            conn.execute(
                "INSERT INTO messages "
                "(id, chat_id, sender, kind, text, poll_id, created_at, media_url, media_type, "
                "direction, wa_status) "
                "VALUES (?, ?, ?, 'image', NULL, NULL, ?, ?, 'image', 'out', 'pending')",
                (msg_id, body.chat_id, body.sender, ts, img_url),
            )
            queued_ids.append(msg_id)
        now = now + timedelta(seconds=1)

    conn.commit()

    if not queued_ids:
        conn.close()
        raise HTTPException(404, "none of the selected listings were found")

    messages = [
        serialize_message(conn, conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone())
        for mid in queued_ids
    ]
    conn.close()

    for m in messages:
        await manager.broadcast(body.chat_id, {"type": "message", "data": m})

    return {"queued": len(messages), "messages": messages}
