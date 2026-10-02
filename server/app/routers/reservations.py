"""routers/reservations.py — book a scraped listing on Airbnb from the app.

Flow
  1. App (house popup, Reserve)   POST /api/reservations          -> status 'pending'
  2. Scraper (PC, logged in)      GET  /api/reservations/pending  -> claims ONE: 'in_progress'
  3. Scraper opens the listing with the dates and guests, clicks Reserve,
     checks the total against what was quoted, writes the message to the
     host, clicks "Request to book"/"Confirm and pay"
                                  POST /api/reservations/{id}/complete
       'requested' -> a WhatsApp message is queued to the chat:
                      "Your reservation has been made … waiting for the host
                       to share the live location"
       'failed'    -> nothing was submitted; safe to try again from the app
       'unknown'   -> the final button WAS clicked but the outcome couldn't
                      be confirmed; check Airbnb Trips by hand. Never retried
                      automatically (a retry could book twice).
       'dry_run'   -> AIRBNB_RESERVE_DRY_RUN on the PC: stopped before the
                      final click (for testing)
  App polls GET /api/reservations/{id} for the status.

Money safety, enforced here and on the PC:
  * one reservation in progress at a time, and a duplicate request for the
    same listing + dates is refused while one is pending/active/requested;
  * a job left 'in_progress' (PC crashed mid-booking) becomes 'unknown',
    never 'pending' again;
  * the PC refuses to submit if Airbnb's total is more than 10% (min $25)
    above the quote, or above AIRBNB_RESERVE_MAX_TOTAL_USD.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ..database import get_db
from ..models import ReservationCompleteIn, ReservationCreateIn, serialize_message
from ..ws_manager import manager
from .houses import _fmt_date, _row_to_listing, ref_code

router = APIRouter(prefix="/api/reservations", tags=["reservations"])

ACTIVE = ("pending", "in_progress", "requested", "unknown")
STALE_AFTER = timedelta(minutes=30)
WA_RESERVED_TEMPLATE = (
    "✅ Your reservation has been made\n"
    "🏠 {ref} · {ci} → {co} ({nights} night{s})\n"
    "Now waiting for the host to share the live location."
)


def _airbnb_id(url: str, listing_id: Optional[str]) -> Optional[str]:
    if listing_id and str(listing_id).isdigit():
        return str(listing_id)
    m = re.search(r"/rooms/(\d+)", url or "")
    return m.group(1) if m else None


def _row(conn, rid: int) -> dict:
    r = conn.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
    if not r:
        raise HTTPException(404, "reservation not found")
    return dict(r)


@router.post("")
def create_reservation(body: ReservationCreateIn):
    conn = get_db()
    try:
        if body.chat_id and not conn.execute("SELECT 1 FROM chats WHERE id=?", (body.chat_id,)).fetchone():
            raise HTTPException(404, "chat not found")
        row = conn.execute("SELECT * FROM house_listings WHERE id=?", (body.listing_id,)).fetchone()
        if not row:
            raise HTTPException(404, "listing not found")
        offer = None
        if body.offer_id:
            offer = conn.execute("SELECT * FROM listing_offers WHERE id=?", (body.offer_id,)).fetchone()
        listing = _row_to_listing(row, offer)

        ci = body.check_in or listing.get("check_in")
        co = body.check_out or listing.get("check_out")
        try:
            d_in, d_out = date.fromisoformat(ci), date.fromisoformat(co)
        except (TypeError, ValueError):
            raise HTTPException(422, "check-in and check-out dates (YYYY-MM-DD) are required to reserve")
        nights = (d_out - d_in).days
        if nights <= 0:
            raise HTTPException(422, "check-out must be after check-in")
        if d_in < datetime.now(timezone.utc).date() - timedelta(days=1):
            raise HTTPException(422, "check-in is in the past")
        airbnb_id = _airbnb_id(listing["url"], listing.get("listing_id"))
        if not airbnb_id:
            raise HTTPException(422, "can't tell this listing's Airbnb room id")

        dup = conn.execute(
            f"SELECT * FROM reservations WHERE listing_url=? AND check_in=? AND check_out=? "
            f"AND status IN ({','.join('?' * len(ACTIVE))}) ORDER BY id DESC LIMIT 1",
            (listing["url"], ci, co, *ACTIVE),
        ).fetchone()
        if dup:
            raise HTTPException(409, f"already {dict(dup)['status']} for these dates (reservation {dict(dup)['id']})")

        # Only trust a per-night price that was scraped for exactly these dates.
        per_night = listing.get("price_usd_per_night") if (listing.get("check_in") == ci
                                                           and listing.get("check_out") == co) else None
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO reservations (chat_id, listing_url, airbnb_id, ref_code, check_in, check_out, "
            "nights, guests, expected_total_usd, message_to_host, status, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (body.chat_id, listing["url"], airbnb_id, ref_code(listing), ci, co, nights, body.guests,
             round(per_night * nights, 2) if per_night else None,
             (body.message or "").strip() or None, body.created_by, now),
        )
        conn.commit()
        return _row(conn, cur.lastrowid)
    finally:
        conn.close()


@router.get("")
def list_reservations(chat_id: Optional[str] = Query(None), limit: int = 20):
    conn = get_db()
    q, args = "SELECT * FROM reservations", []
    if chat_id:
        q, args = q + " WHERE chat_id=?", [chat_id]
    rows = conn.execute(q + " ORDER BY id DESC LIMIT ?", (*args, max(1, min(limit, 100)))).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/pending")
def claim_pending():
    """Hand the PC at most ONE reservation, and only when none is running."""
    conn = get_db()
    try:
        now = datetime.now(timezone.utc)
        conn.execute("BEGIN IMMEDIATE")
        for r in conn.execute("SELECT id, started_at FROM reservations WHERE status='in_progress'").fetchall():
            try:
                started = datetime.fromisoformat(r["started_at"])
            except (TypeError, ValueError):
                started = now
            if now - started > STALE_AFTER:
                conn.execute(
                    "UPDATE reservations SET status='unknown', completed_at=?, error_message=? WHERE id=?",
                    (now.isoformat(), "PC stopped mid-booking — check Airbnb Trips before retrying", r["id"]))
        if conn.execute("SELECT 1 FROM reservations WHERE status='in_progress'").fetchone():
            conn.commit()
            return []
        job = conn.execute(
            "SELECT * FROM reservations WHERE status='pending' ORDER BY id ASC LIMIT 1").fetchone()
        if not job:
            conn.commit()
            return []
        conn.execute("UPDATE reservations SET status='in_progress', started_at=? WHERE id=?",
                     (now.isoformat(), job["id"]))
        conn.commit()
        return [_row(conn, job["id"])]
    finally:
        conn.close()


@router.get("/{rid}")
def get_reservation(rid: int):
    conn = get_db()
    try:
        return _row(conn, rid)
    finally:
        conn.close()


@router.post("/{rid}/cancel")
def cancel_reservation(rid: int):
    """Only a request the PC hasn't picked up yet can be cancelled here."""
    conn = get_db()
    try:
        r = _row(conn, rid)
        if r["status"] != "pending":
            raise HTTPException(409, f"can't cancel a reservation that is {r['status']}")
        conn.execute("UPDATE reservations SET status='cancelled', completed_at=? WHERE id=?",
                     (datetime.now(timezone.utc).isoformat(), rid))
        conn.commit()
        return _row(conn, rid)
    finally:
        conn.close()


@router.post("/{rid}/complete")
async def complete_reservation(rid: int, body: ReservationCompleteIn):
    conn = get_db()
    queued = None
    try:
        r = _row(conn, rid)
        if r["status"] != "in_progress":
            raise HTTPException(409, f"reservation is {r['status']}, not in progress")
        now = datetime.now(timezone.utc)
        conn.execute(
            "UPDATE reservations SET status=?, total_usd=?, trip_url=?, error_message=?, "
            "cancellation_policy=?, completed_at=? WHERE id=?",
            (body.status, body.total_usd, body.trip_url, body.error_message,
             body.cancellation_policy, now.isoformat(), rid))
        if body.status == "requested" and r["chat_id"]:
            text = WA_RESERVED_TEMPLATE.format(
                ref=r["ref_code"] or "your stay", ci=_fmt_date(r["check_in"]), co=_fmt_date(r["check_out"]),
                nights=r["nights"], s="s" if r["nights"] != 1 else "")
            mid = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO messages (id, chat_id, sender, kind, text, poll_id, created_at, "
                "direction, wa_status) VALUES (?, ?, 'Front Desk', 'text', ?, NULL, ?, 'out', 'pending')",
                (mid, r["chat_id"], text, now.isoformat()))
            queued = serialize_message(conn, conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone())
        conn.commit()
        result = _row(conn, rid)
    finally:
        conn.close()
    if queued:
        await manager.broadcast(r["chat_id"], {"type": "message", "data": queued})
    return result
