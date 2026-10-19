"""davis/catalogue.py — the live catalogue, as Davis uses it: which houses to
offer a guest (from the same listings and dated offers the app's house
search shows), the short card each one is sent with, and which option a
guest means by "KCER 248", "option 2" or "the second one"."""

from __future__ import annotations

import re
import time
from datetime import date
from typing import Optional

from ..routers import houses
from . import config, nlu, store

_cache: dict[tuple, tuple[float, list]] = {}


def _cached_offers(city: str, check_in: str, check_out: str) -> list[dict]:
    """Dated offers for (city, dates). Cached briefly: a burst of guests asking
    about Harare this weekend costs one query, not one each."""
    key = (city, check_in, check_out)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < config.CACHE_TTL_SEC:
        return hit[1]
    rows = houses.available_listings(location=city, check_in=check_in, check_out=check_out,
                                     limit=houses.MAX_AVAILABLE_LIMIT, sort="newest", chat_id=None)
    if rows:                      # an empty answer may fill any second (live re-price)
        _cache[key] = (time.monotonic(), rows)
    if len(_cache) > 256:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:64]:
            _cache.pop(k, None)
    return rows


def clear_cache() -> None:
    _cache.clear()


def places(conn) -> dict[str, str]:
    """Suburb -> city from the scraped listings ("greendale" -> "harare")."""
    out = {}
    for r in conn.execute("SELECT DISTINCT neighbourhood, location FROM house_listings "
                          "WHERE neighbourhood IS NOT NULL AND neighbourhood != ''"):
        out[r["neighbourhood"].strip().lower()] = (r["location"] or "").strip().lower()
    for r in conn.execute("SELECT DISTINCT location FROM house_listings"):
        if r["location"]:
            out[r["location"].strip().lower()] = r["location"].strip().lower()
    return out


def city_has_listings(conn, city: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM house_listings WHERE location=? LIMIT 1", (city,)).fetchone())


def capacity_guests(capacity: Optional[str]) -> Optional[int]:
    m = re.search(r"(\d+)\s*\+?\s*guests?", capacity or "")
    return int(m.group(1)) if m else None


def capacity_bedrooms(capacity: Optional[str]) -> Optional[int]:
    m = re.search(r"(\d+)\s*bedrooms?", capacity or "")
    return int(m.group(1)) if m else None


def _rating(d: dict) -> float:
    try:
        return float(d.get("rating") or 0)
    except ValueError:
        return 0.0


def sent_urls(conn, chat_id: str) -> set[str]:
    return {r["listing_url"] for r in conn.execute("SELECT listing_url FROM house_sends WHERE chat_id=?", (chat_id,))}


def find_options(conn, chat_id: str, slots: dict, n: int = 0, exclude_sent: bool = True) -> tuple[list[dict], str]:
    """Up to n houses for the guest's city and dates. Status:
    ok            options found
    no_dates      the guest hasn't given dates yet
    none_dated    the city has houses but none priced for these dates yet
                  (ask the scraper for a live re-price)
    no_listings   nothing at all in that city
    all_sent      everything matching was already sent to this guest
    Ranked: featured first, then houses that fit the party, then rating;
    hidden houses never. A budget narrows the list unless nothing fits it."""
    n = n or config.OPTIONS_PER_SEND
    city, ci, co = slots.get("city"), slots.get("check_in"), slots.get("check_out")
    if not (ci and co):
        return [], "no_dates"
    rows = [r for r in _cached_offers(city, ci, co) if priced(r)]
    if not rows:
        return [], ("none_dated" if city_has_listings(conn, city) else "no_listings")
    flags = store.catalogue_flags(conn)
    rows = [r for r in rows if not flags.get(r["url"], {}).get("hidden")]
    if exclude_sent:
        done = sent_urls(conn, chat_id)
        fresh = [r for r in rows if r["url"] not in done]
        if rows and not fresh:
            return [], "all_sent"
        rows = fresh
    guests = slots.get("guests")
    beds = slots.get("bedrooms")
    status = "ok"
    if guests:
        # A home that sleeps the party; else one whose size isn't listed; else
        # the biggest, said honestly (15 guests were sent homes for 6-8).
        fits = [r for r in rows if (capacity_guests(r.get("capacity")) or 0) >= guests]
        unknown = [r for r in rows if capacity_guests(r.get("capacity")) is None]
        if fits:
            rows = fits
        elif unknown:
            rows = unknown
        else:
            rows = sorted(rows, key=lambda r: -(capacity_guests(r.get("capacity")) or 0))[:n]
            status = "too_small"
    if beds:
        fits = [r for r in rows if (capacity_bedrooms(r.get("capacity")) or 0) >= beds]
        rows = fits or rows
    budget = slots.get("budget_usd")
    if budget:
        per_night = budget / slots["nights"] if slots.get("budget_basis") == "total" and slots.get("nights") else budget
        within = [r for r in rows if r.get("price_usd_per_night") and r["price_usd_per_night"] <= per_night * 1.1]
        rows = within or sorted(rows, key=lambda r: r.get("price_usd_per_night") or 1e9)
    if status == "too_small":
        return rows[:n], status
    rows.sort(key=lambda r: (not flags.get(r["url"], {}).get("featured"),
                             not r.get("images"),
                             -_rating(r)))
    return rows[:n], status


def priced(r: dict) -> bool:
    """A real price for the dates: USD per night, or a shown price with an
    amount above zero ("Pay $0 today" is a payment plan, not a price)."""
    if r.get("price_usd_per_night"):
        return r["price_usd_per_night"] > 0
    raw = r.get("price_raw") or ""
    return any(float(x.replace(",", "")) > 0 for x in re.findall(r"\d[\d,]*(?:\.\d+)?", raw)) \
        and not re.search(r"pay\s*\$?\s*0\b", raw, re.I)


def compact_card(listing: dict, option: int = 1, of: int = 1) -> str:
    """The text sent before a house's photos: Davis's version of
    houses.build_listing_message, never more than MAX_WORDS words."""
    lines = [f"*Option {option} · {houses.ref_code(listing)}*" if of > 1 else f"*{houses.ref_code(listing)}*"]
    place = houses.place_of(listing)
    if place:
        lines.append(f"📍 {place}")
    cap = listing.get("capacity") or ""
    if cap:
        parts = [p.strip() for p in cap.split("·")]
        lines.append("👥 " + " · ".join(parts[:2]))
    if listing.get("check_in") and listing.get("check_out"):
        nights = listing.get("nights")
        lines.append(f"📅 {_short(listing['check_in'])} → {_short(listing['check_out'])}"
                     + (f" ({nights} night{'s' if nights != 1 else ''})" if nights else ""))
    usd, nights = listing.get("price_usd_per_night"), listing.get("nights")
    if usd:
        due = amount_due(usd, nights)
        lines.append(f"💵 USD {usd:,.2f}/night" + (f" · USD {due:,.2f} {fee_label()}" if due else ""))
    elif listing.get("price_raw") and priced(listing):
        lines.append(f"💵 {listing['price_raw']}")
    if listing.get("rating"):
        lines.append(f"⭐ {listing['rating']}")
    text = "\n".join(lines)
    while nlu.word_count(text) > config.MAX_WORDS and len(lines) > 2:
        lines.pop()                     # rating, then price-less extras go first
        text = "\n".join(lines)
    return text


def _short(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
        return f"{d.day} {d.strftime('%b')}"
    except ValueError:
        return iso


def fmt_range(ci: Optional[str], co: Optional[str]) -> str:
    """"11–14 Dec", "28 Dec–2 Jan"."""
    if not (ci and co):
        return ""
    try:
        a, b = date.fromisoformat(ci), date.fromisoformat(co)
    except ValueError:
        return f"{ci}–{co}"
    if (a.year, a.month) == (b.year, b.month):
        return f"{a.day}–{b.day} {b.strftime('%b')}"
    return f"{_short(ci)}–{_short(co)}"


def amount_due(rate: Optional[float], nights: Optional[int]) -> Optional[float]:
    """What the invoice's Balance Due will say: nights × rate plus the service
    fee, rounded exactly as invoice_worker.py rounds it."""
    if not rate or not nights:
        return None
    subtotal = round(rate * nights, 2)
    return round(subtotal + round(subtotal * config.SERVICE_FEE_PCT / 100, 2), 2)


def fee_label() -> str:
    return f"incl. {config.SERVICE_FEE_PCT:g}% service fee" if config.SERVICE_FEE_PCT else "total"


def option_facts(listing: dict) -> dict:
    """The facts about one offered house Davis may quote (and nothing else)."""
    usd, nights = listing.get("price_usd_per_night"), listing.get("nights")
    return {
        "ref": houses.ref_code(listing), "ref_no": listing.get("ref_no"), "url": listing.get("url"),
        "offer_id": listing.get("offer_id"), "option_no": listing.get("option_no"),
        "place": houses.place_of(listing), "location": listing.get("location"),
        "capacity": listing.get("capacity"), "rating": listing.get("rating"),
        "check_in": listing.get("check_in"), "check_out": listing.get("check_out"), "nights": nights,
        "usd_per_night": usd, "usd_total": round(usd * nights, 2) if usd and nights else None,
        "usd_due": amount_due(usd, nights),
        "sent_at": listing.get("sent_at"),
    }


def with_notes(conn, facts: list[dict]) -> list[dict]:
    """Add the team's note for each house (amenities, rules: set on the
    dashboard) so Davis can answer "is there wifi?" from it."""
    flags = store.catalogue_flags(conn)
    for f in facts:
        note = (flags.get(f.get("url")) or {}).get("note")
        if note:
            f["note"] = note
    return facts


def offered(conn, chat_id: str, limit: int = 12) -> list[dict]:
    """Every house already sent to this chat (by Davis or by staff from the
    app), latest batch first — what "option 2" and "KCER 248" refer to."""
    return with_notes(conn, [option_facts(d) for d in houses.sent_listings(conn, chat_id, limit)])


def resolve_choice(conn, feats: dict, offers: list[dict], slots: dict, stage: str) -> Optional[dict]:
    """Which house the guest picked, if this message picks one."""
    latest = [o for o in offers if offers and o.get("sent_at") == offers[0].get("sent_at")]
    latest.sort(key=lambda o: o.get("option_no") or 0)
    if feats.get("option_ref"):
        ref_no = feats["option_ref"]
        hit = next((o for o in offers if o.get("ref_no") == ref_no), None)
        if hit:
            return hit
        row = conn.execute("SELECT * FROM house_listings WHERE ref_no=?", (ref_no,)).fetchone()
        if not row:
            return None
        offer = None
        if slots.get("check_in") and slots.get("check_out"):
            offer = conn.execute("SELECT * FROM listing_offers WHERE id=?",
                                 (houses.offer_id_for(row["id"], slots["check_in"], slots["check_out"]),)).fetchone()
        d = option_facts(houses._row_to_listing(row, offer))
        if not offer:                      # no price for the guest's dates: don't quote the stale one
            d.update(usd_per_night=None, usd_total=None, usd_due=None, offer_id=None,
                     check_in=slots.get("check_in"), check_out=slots.get("check_out"), nights=slots.get("nights"))
        return d
    no = feats.get("option_no")
    if no is None and feats.get("bare_number") and stage == "present":
        no = feats["bare_number"]
    if no is not None and latest:
        if no == -1:
            return latest[-1]
        return next((o for o in latest if o.get("option_no") == no), None)
    if feats.get("affirm") and stage == "present" and len(latest) == 1:
        return latest[0]
    return None
