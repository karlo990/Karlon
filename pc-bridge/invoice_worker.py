"""invoice_worker.py — renders pending Karlon invoices to PDF.

Runs as a separate, always-on process on the same PC as wa_bridge.py
(same LAN as the Karlon server, or pointed at the HF Space via
KARLON_URL — same env var wa_bridge.py uses). Deliberately has NO
Playwright dependency: it only builds a .docx with python-docx and
shells out to LibreOffice to convert it to PDF. WhatsApp delivery of
the finished PDF is handled entirely by wa_bridge.py's existing outbox
poller — see routers/invoices.py's upload_invoice_pdf() for how the
hand-off happens (it inserts a normal outbox message once the PDF is
uploaded here).

REQUIRES:
    pip install python-docx requests
    LibreOffice installed and `soffice` on PATH
        Windows default: C:\\Program Files\\LibreOffice\\program\\soffice.exe
        (set LIBREOFFICE_PATH below if it's not on PATH)

RUN:
    python invoice_worker.py                  # poll forever, every 5s
    KARLON_URL=http://localhost:8000 python invoice_worker.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt

from io import BytesIO

from karlon_client import DOWNLOAD_TIMEOUT, HTTP_TIMEOUT, UPLOAD_TIMEOUT, get_session
from local_config import KARLON_URL, INVOICE_POLL_SEC, LIBREOFFICE_PATH, print_active_config

# PowerShell's cp1252 console can't encode the ✓/✗ this worker prints; without
# this, a *successful* render still crashed the poll loop on the ✓ print with
# "'charmap' codec can't encode character '\u2717'". Reconfigure to UTF-8.
import sys
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

POLL_INTERVAL_SEC = INVOICE_POLL_SEC

# Timeouts, retry policy and the pooled session live in karlon_client.py so
# wa_bridge.py and this worker can't drift apart again.


# ─────────────────────────────── docx generation ───────────────────────────────

def _fetch_listing_photo(invoice: dict) -> BytesIO | None:
    """First photo of the linked listing (listing_images[0]) as an in-memory
    stream, so the invoice shows the property. Server paths ('/static/...')
    are resolved against KARLON_URL; absolute URLs are fetched as-is. Best-
    effort — any failure just omits the photo."""
    images = invoice.get("listing_images") or []
    if not images:
        return None
    url = images[0]
    full = url if url.startswith("http") else f"{KARLON_URL}{url}"
    try:
        r = get_session().get(full, timeout=DOWNLOAD_TIMEOUT)
        r.raise_for_status()
        if r.content and "image" in r.headers.get("content-type", "image"):
            return BytesIO(r.content)
    except Exception as e:
        print(f"[invoice_worker] listing photo fetch failed: {e}")
    return None


def build_invoice_docx(invoice: dict, out_path: Path) -> None:
    """
    Builds a print-ready invoice from booking data. When the invoice was
    generated from a scraped listing, the linked property's photo and its
    ZAR->USD price breakdown are included; the headline figure is always the
    USD price the rest of the app uses.
    """
    doc = Document()

    title = doc.add_heading("KARLCON — Guest Invoice", level=1)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    meta = doc.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = meta.add_run(f"Invoice #{invoice['id'][:8].upper()}")
    run.font.size = Pt(10)
    run.italic = True

    # Property photo from the linked listing (if any).
    photo = _fetch_listing_photo(invoice)
    if photo is not None:
        try:
            pic_p = doc.add_paragraph()
            pic_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            pic_p.add_run().add_picture(photo, width=Inches(4.5))
        except Exception as e:
            print(f"[invoice_worker] couldn't embed listing photo: {e}")

    doc.add_paragraph()

    table = doc.add_table(rows=0, cols=2)
    table.style = "Light Grid Accent 1"

    def row(label: str, value: str) -> None:
        cells = table.add_row().cells
        cells[0].text = label
        cells[1].text = value or "-"

    currency = invoice.get("currency", "USD")
    rate = invoice.get("rate", 0) or 0
    total = invoice.get("total", 0) or 0

    row("Guest name", invoice.get("guest_name", ""))
    row("ID / Passport number", invoice.get("id_number", ""))
    row("Location", (invoice.get("location") or "").title())
    row("Property", invoice.get("listing_title") or invoice.get("property_name", ""))
    row("Check-in", invoice.get("check_in") or "-")
    row("Check-out", invoice.get("check_out") or "-")
    row("Nights", str(invoice.get("nights", 1)))
    row("Rate per night", f"{currency} {rate:,.2f}")

    # ZAR original + FX, shown only when the listing was priced in ZAR — this
    # is the audit trail for how the USD figure was derived.
    zar = invoice.get("price_zar_per_night")
    fx = invoice.get("fx_rate_zar_per_usd")
    if zar:
        row("Listing price (ZAR)", f"R {zar:,.2f} / night")
    if zar and fx:
        row("Exchange rate applied", f"1 USD = R {fx:,.4f}")

    row("Total due", f"{currency} {total:,.2f}")

    doc.add_paragraph()
    footer = doc.add_paragraph("Thank you for booking with KARLCON Elite Retreats.")
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER

    doc.save(out_path)


def convert_docx_to_pdf(docx_path: Path, out_dir: Path) -> Path:
    """Shells out to LibreOffice headless. Raises on any failure."""
    soffice = shutil.which("soffice") or (
        LIBREOFFICE_PATH if Path(LIBREOFFICE_PATH).exists() else None
    )
    if not soffice:
        raise RuntimeError(
            "LibreOffice ('soffice') not found on PATH and LIBREOFFICE_PATH "
            f"({LIBREOFFICE_PATH}) doesn't exist — install LibreOffice or "
            "set LIBREOFFICE_PATH to its soffice executable."
        )

    # Private LibreOffice profile per conversion. With the default shared
    # profile, a second soffice (or an open LibreOffice window on this PC)
    # holds the profile lock and headless convert exits 0 without producing a
    # PDF — an intermittent "conversion failed" that looks unrelated to us.
    profile_uri = (out_dir / "lo_profile").as_uri()
    result = subprocess.run(
        [soffice, f"-env:UserInstallation={profile_uri}", "--headless",
         "--convert-to", "pdf", "--outdir", str(out_dir), str(docx_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    pdf_path = out_dir / (docx_path.stem + ".pdf")
    if result.returncode != 0 or not pdf_path.exists():
        raise RuntimeError(
            f"soffice conversion failed (rc={result.returncode}): "
            f"{result.stdout}\n{result.stderr}"
        )
    return pdf_path


# ─────────────────────────────── server calls ──────────────────────────────────

def fetch_pending() -> list[dict]:
    r = get_session().get(f"{KARLON_URL}/api/invoices/worker/pending", timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def claim(invoice_id: str) -> bool:
    r = get_session().patch(f"{KARLON_URL}/api/invoices/{invoice_id}/claim", timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return bool(r.json().get("claimed"))


def upload_pdf(invoice_id: str, pdf_path: Path) -> None:
    with open(pdf_path, "rb") as f:
        r = get_session().post(
            f"{KARLON_URL}/api/invoices/{invoice_id}/pdf",
            files={"file": (pdf_path.name, f, "application/pdf")},
            timeout=UPLOAD_TIMEOUT,
        )
    r.raise_for_status()


def mark_error(invoice_id: str, message: str) -> None:
    try:
        get_session().patch(
            f"{KARLON_URL}/api/invoices/{invoice_id}/error",
            json={"error_message": message[:500]},
            timeout=HTTP_TIMEOUT,
        )
    except Exception as e:
        print(f"[invoice_worker] also failed to report error: {e}")


# ─────────────────────────────── main loop ─────────────────────────────────────

def process_one(invoice: dict) -> None:
    invoice_id = invoice["id"]
    print(f"[invoice_worker] claiming {invoice_id} ({invoice.get('guest_name')})...")
    if not claim(invoice_id):
        print(f"[invoice_worker] {invoice_id} already claimed elsewhere, skipping")
        return

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        docx_path = tmp_dir / f"invoice_{invoice_id}.docx"
        try:
            build_invoice_docx(invoice, docx_path)
            pdf_path = convert_docx_to_pdf(docx_path, tmp_dir)
            upload_pdf(invoice_id, pdf_path)
            print(f"[invoice_worker] ✓ {invoice_id} rendered and uploaded")
        except Exception as e:
            print(f"[invoice_worker] ✗ {invoice_id} failed: {e}")
            mark_error(invoice_id, str(e))


def main() -> None:
    print_active_config()
    print(f"[invoice_worker] polling {KARLON_URL} every {POLL_INTERVAL_SEC}s")
    while True:
        try:
            pending = fetch_pending()
            for invoice in pending:
                process_one(invoice)
        except Exception as e:
            print(f"[invoice_worker] poll error: {e}")
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
