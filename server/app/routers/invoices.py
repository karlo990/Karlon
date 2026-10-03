"""routers/invoices.py — guest invoice generation.

Flow
====
1. App POSTs /api/invoices (guest name, ID, location, property, dates,
   rate) -> row created with status='pending'.
2. invoice_worker.py (runs on the PC beside wa_bridge.py) polls
   GET /api/invoices/worker/pending every few seconds, claims one via
   PATCH /{id}/claim, builds a .docx from the booking data, converts it
   to PDF (LibreOffice headless), then POSTs the PDF bytes to
   POST /{id}/pdf.
3. That upload saves the PDF under static/invoices/, marks the invoice
   'ready', and — if the invoice has a chat_id — inserts a normal
   direction='out' / wa_status='pending' message row pointing at the
   PDF. That message rides the *existing* /api/outbox pipeline, so
   wa_bridge's regular outbox poller picks it up and Playwright-attaches
   it to WhatsApp exactly like any other outbound message — no separate
   delivery mechanism needed.
"""

import json
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from ..config import INVOICES_DIR, PROPERTY_CATALOGUE
from ..database import get_db
from ..models import InvoiceCreateIn, InvoiceErrorIn, serialize_message
from ..ws_manager import manager
from .houses import _row_to_listing

router = APIRouter(prefix="/api/invoices", tags=["invoices"])


# ─────────────────────────── property catalogue (dropdowns) ───────────────────

@router.get("/locations")
def list_locations():
    """Shape the app's two dependent dropdowns need: location -> properties.
    The static PROPERTY_CATALOGUE is merged with every city the scraper has
    pushed listings for, so a listing chosen from a live search always has
    its city available in the Location dropdown (properties for those cities
    come from GET /api/houses/available, not this catalogue)."""
    out = {loc: list(props) for loc, props in PROPERTY_CATALOGUE.items()}
    conn = get_db()
    rows = conn.execute("SELECT DISTINCT location FROM house_listings").fetchall()
    conn.close()
    for r in rows:
        pretty = (r["location"] or "").strip().title()
        if pretty and pretty not in out:
            out[pretty] = []
    return [{"location": loc, "properties": props} for loc, props in sorted(out.items())]


def invoice_number(d: dict) -> str:
    """KCER-<year>-<0001>: the number printed on the invoice and its file name."""
    year = (d.get("created_at") or datetime.now(timezone.utc).isoformat())[:4]
    if not d.get("invoice_no"):                     # created before numbering existed
        return f"KCER-{year}-{str(d.get('id') or '')[:6].upper()}"
    return f"KCER-{year}-{int(d['invoice_no']):04d}"


def _invoice_row(row) -> dict:
    d = dict(row)
    d["invoice_number"] = invoice_number(d)
    d["guests"] = d.get("guests") or 1
    try:
        d["listing_images"] = json.loads(d.get("listing_images") or "[]")
    except Exception:
        d["listing_images"] = []
    return d


# ─────────────────────────── create / list ─────────────────────────────────────

@router.post("")
def create_invoice(body: InvoiceCreateIn):
    if body.chat_id:
        conn = get_db()
        exists = conn.execute("SELECT 1 FROM chats WHERE id=?", (body.chat_id,)).fetchone()
        conn.close()
        if not exists:
            raise HTTPException(404, "chat not found")

    nights = max(1, body.nights)
    # If both dates are valid, nights is derived from them (the app also does
    # this — belt and braces so the PDF never disagrees with the dates).
    if body.check_in and body.check_out:
        try:
            d = (date.fromisoformat(body.check_out) - date.fromisoformat(body.check_in)).days
            if d >= 1:
                nights = d
        except ValueError:
            pass

    conn = get_db()

    # ── listing link (feature: listing metadata -> invoice auto-population) ──
    listing_title = listing_images = zar = fx = None
    listing_url = body.listing_url
    offer_id = body.listing_offer_id
    rate = body.rate
    if listing_url:
        lrow = conn.execute("SELECT * FROM house_listings WHERE id=?", (listing_url,)).fetchone()
        if lrow:
            offer = None
            if offer_id:
                offer = conn.execute("SELECT * FROM listing_offers WHERE id=?", (offer_id,)).fetchone()
            listing = _row_to_listing(lrow, offer)
            listing_title = listing.get("title") or None
            listing_images = json.dumps(listing.get("images") or [])
            zar = listing.get("price_zar_per_night")
            fx = listing.get("fx_rate_zar_per_usd")
            if not rate and listing.get("price_usd_per_night"):
                rate = float(listing["price_usd_per_night"])
        else:
            listing_url = None  # unknown listing — don't store a dangling link

    total = round(rate * nights, 2)
    inv_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    invoice_no = conn.execute("SELECT COALESCE(MAX(invoice_no), 0) + 1 FROM invoices").fetchone()[0]
    conn.execute(
        "INSERT INTO invoices "
        "(id, chat_id, guest_name, id_number, location, property_name, check_in, "
        "check_out, nights, rate, total, currency, status, created_by, created_at, updated_at, "
        "listing_url, listing_offer_id, listing_title, listing_images, price_zar_per_night, fx_rate_zar_per_usd, "
        "invoice_no, guests) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (inv_id, body.chat_id, body.guest_name.strip(), body.id_number.strip(),
         body.location.strip(), body.property_name.strip(), body.check_in, body.check_out,
         nights, rate, total, body.currency, body.created_by.strip(), now, now,
         listing_url, offer_id, listing_title, listing_images, zar, fx, invoice_no, body.guests),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM invoices WHERE id=?", (inv_id,)).fetchone()
    conn.close()
    return _invoice_row(row)


@router.get("")
def list_invoices():
    conn = get_db()
    rows = conn.execute(
        """
        SELECT i.*, c.name AS chat_name
        FROM invoices i
        LEFT JOIN chats c ON c.id = i.chat_id
        ORDER BY i.created_at DESC
        """
    ).fetchall()
    conn.close()
    return [_invoice_row(r) for r in rows]


# ─────────────────────────── worker hand-off ───────────────────────────────────
# NOTE: this MUST be declared before the "/{invoice_id}" route below. FastAPI
# matches routes in declaration order, so if "/{invoice_id}" comes first it
# greedily captures "/worker/pending" as invoice_id="worker" (well, tries to
# match "pending" as a sub-path and 404s) — that's what caused
# invoice_worker.py's poll loop to get a 404 on every request in production.

@router.get("/worker/pending")
def worker_pending():
    """Polled by invoice_worker.py. Returns invoices awaiting PDF generation."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM invoices WHERE status='pending' ORDER BY created_at ASC"
    ).fetchall()
    conn.close()
    return [_invoice_row(r) for r in rows]


@router.get("/{invoice_id}")
def get_invoice(invoice_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "invoice not found")
    return _invoice_row(row)


@router.patch("/{invoice_id}/claim")
def claim_invoice(invoice_id: str):
    """
    Atomically flips pending -> processing so two worker instances (or a
    restarted worker mid-poll) never render the same invoice twice.
    Returns claimed=False if someone/something else already has it.
    """
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "UPDATE invoices SET status='processing', updated_at=? "
        "WHERE id=? AND status='pending'",
        (now, invoice_id),
    )
    conn.commit()
    claimed = cur.rowcount > 0
    row = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "invoice not found")
    return {"claimed": claimed, "invoice": _invoice_row(row)}


@router.post("/{invoice_id}/pdf")
async def upload_invoice_pdf(invoice_id: str, file: UploadFile = File(...)):
    conn = get_db()
    inv = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not inv:
        conn.close()
        raise HTTPException(404, "invoice not found")

    data = await file.read()
    if not data:
        conn.close()
        raise HTTPException(400, "empty file")

    # The file name is what the guest sees on the document in WhatsApp.
    fname = f"KARLCON_Invoice_{invoice_number(dict(inv))}.pdf"
    (INVOICES_DIR / fname).write_bytes(data)
    pdf_url = f"/static/invoices/{fname}"

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE invoices SET status='ready', pdf_url=?, updated_at=? WHERE id=?",
        (pdf_url, now, invoice_id),
    )
    conn.commit()

    msg = None
    chat_id = inv["chat_id"]
    if chat_id:
        msg_id = str(uuid.uuid4())
        caption = f"Invoice {invoice_number(dict(inv))} — {inv['guest_name']}"
        conn.execute(
            "INSERT INTO messages "
            "(id, chat_id, sender, kind, text, poll_id, created_at, media_url, media_type, "
            "direction, wa_status) "
            "VALUES (?, ?, ?, 'document', ?, NULL, ?, ?, 'document', 'out', 'documents')",
            (msg_id, chat_id, inv["created_by"] or "Front Desk", caption, now, pdf_url),
        )
        conn.commit()
        msg = serialize_message(
            conn, conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
        )

    row = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    conn.close()

    if msg:
        await manager.broadcast(chat_id, {"type": "message", "data": msg})

    return _invoice_row(row)


@router.patch("/{invoice_id}/error")
def mark_invoice_error(invoice_id: str, body: InvoiceErrorIn):
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE invoices SET status='error', error_message=?, updated_at=? WHERE id=?",
        (body.error_message, now, invoice_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "invoice not found")
    return _invoice_row(row)

@router.post("/{invoice_id}/retry")
def retry_invoice(invoice_id: str):
    """Puts a failed invoice (or one stuck in 'processing' after a worker
    crash) back in the queue, so invoice_worker.py renders it again."""
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "UPDATE invoices SET status='pending', error_message=NULL, updated_at=? "
        "WHERE id=? AND status IN ('error', 'processing')",
        (now, invoice_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "invoice not found")
    if not cur.rowcount:
        raise HTTPException(409, f"invoice is {row['status']}, only failed or stuck invoices can be retried")
    return _invoice_row(row)
