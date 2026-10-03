"""invoice_worker.py — renders pending Karlon invoices to PDF.

Runs as a separate, always-on process on the same PC as wa_bridge.py
(same LAN as the Karlon server, or pointed at the HF Space via
KARLON_URL — same env var wa_bridge.py uses). It builds the invoice and
turns it into a PDF with the first of these that works on this PC:
  1. LibreOffice (soffice), converting a .docx built with python-docx
  2. Microsoft Word, converting the same .docx (driven via PowerShell)
  3. Chromium (already installed for wa_bridge.py via Playwright), printing
     an HTML version of the same invoice — needs nothing else installed.
WhatsApp delivery of
the finished PDF is handled entirely by wa_bridge.py's existing outbox
poller — see routers/invoices.py's upload_invoice_pdf() for how the
hand-off happens (it inserts a normal outbox message once the PDF is
uploaded here).

REQUIRES:
    pip install python-docx requests playwright
    Optional: LibreOffice or Microsoft Word for the .docx look; without
    either, Chromium renders the PDF. Force one with INVOICE_PDF_ENGINE=
    libreoffice | word | chromium.

RUN:
    python invoice_worker.py                  # poll forever, every 5s
    KARLON_URL=http://localhost:8000 python invoice_worker.py
"""

from __future__ import annotations

import base64
import html
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


def build_invoice_docx(invoice: dict, out_path: Path, photo: BytesIO | None = None) -> None:
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
    if photo is None:
        photo = _fetch_listing_photo(invoice)
    if photo is not None:
        photo.seek(0)
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


_SOFFICE_CANDIDATES = [
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/usr/bin/soffice", "/usr/bin/libreoffice", "/Applications/LibreOffice.app/Contents/MacOS/soffice",
]


def find_soffice() -> str | None:
    for cand in (shutil.which("soffice"), shutil.which("libreoffice"), LIBREOFFICE_PATH, *_SOFFICE_CANDIDATES):
        if cand and Path(cand).exists():
            return cand
    return None


def convert_docx_to_pdf(docx_path: Path, out_dir: Path) -> Path:
    """Shells out to LibreOffice headless. Raises on any failure."""
    soffice = find_soffice()
    if not soffice:
        raise RuntimeError("LibreOffice ('soffice') not found")

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


def convert_docx_with_word(docx_path: Path, out_dir: Path) -> Path:
    """Microsoft Word through its COM interface, driven from PowerShell (no
    pywin32 needed). Windows with Word installed only."""
    if os.name != "nt" or not shutil.which("powershell"):
        raise RuntimeError("Microsoft Word conversion needs Windows")
    pdf_path = out_dir / (docx_path.stem + ".pdf")
    q = lambda p: str(p).replace("'", "''")    # PowerShell single-quoted string  # noqa: E731
    script = (
        "$ErrorActionPreference='Stop';"
        "$w=New-Object -ComObject Word.Application;$w.Visible=$false;$w.DisplayAlerts=0;"
        f"try{{$d=$w.Documents.Open('{q(docx_path)}',$false,$true);"
        f"$d.SaveAs([ref]'{q(pdf_path)}',[ref]17);$d.Close($false)}}finally{{$w.Quit()}}"
    )
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, timeout=120)
    if result.returncode != 0 or not pdf_path.exists():
        raise RuntimeError(f"Word conversion failed: {(result.stderr or result.stdout).strip()[:300]}")
    return pdf_path


def build_invoice_html(invoice: dict, photo: BytesIO | None = None) -> str:
    """The same invoice as build_invoice_docx, as a printable HTML page."""
    esc = lambda v: html.escape(str(v if v not in (None, "") else "-"))  # noqa: E731
    currency = invoice.get("currency", "USD")
    rate = invoice.get("rate", 0) or 0
    total = invoice.get("total", 0) or 0
    rows = [
        ("Guest name", invoice.get("guest_name", "")),
        ("ID / Passport number", invoice.get("id_number", "")),
        ("Location", (invoice.get("location") or "").title()),
        ("Property", invoice.get("listing_title") or invoice.get("property_name", "")),
        ("Check-in", invoice.get("check_in") or "-"),
        ("Check-out", invoice.get("check_out") or "-"),
        ("Nights", str(invoice.get("nights", 1))),
        ("Rate per night", f"{currency} {rate:,.2f}"),
    ]
    zar, fx = invoice.get("price_zar_per_night"), invoice.get("fx_rate_zar_per_usd")
    if zar:
        rows.append(("Listing price (ZAR)", f"R {zar:,.2f} / night"))
    if zar and fx:
        rows.append(("Exchange rate applied", f"1 USD = R {fx:,.4f}"))
    table = "".join(f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in rows)
    table += f'<tr class="total"><th>Total due</th><td>{esc(f"{currency} {total:,.2f}")}</td></tr>'
    img = ""
    if photo is not None:
        data = photo.getvalue()
        mime = "image/png" if data[:4] == b"\x89PNG" else "image/webp" if data[8:12] == b"WEBP" else "image/jpeg"
        img = f'<img src="data:{mime};base64,{base64.b64encode(data).decode()}">'
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
      body {{ font-family: Calibri, Arial, sans-serif; color: #1f2937; margin: 0; }}
      h1 {{ text-align: center; color: #1f4e79; font-size: 22pt; margin: 0 0 4pt; }}
      .meta {{ text-align: center; font-style: italic; font-size: 10pt; color: #555; margin-bottom: 14pt; }}
      img {{ display: block; margin: 0 auto 16pt; max-width: 4.5in; max-height: 3.2in; border-radius: 6pt; }}
      table {{ width: 100%; border-collapse: collapse; font-size: 11pt; }}
      th, td {{ border: 1px solid #9dc3e6; padding: 6pt 8pt; text-align: left; vertical-align: top; }}
      th {{ width: 40%; background: #deebf7; font-weight: 600; }}
      tr.total th, tr.total td {{ font-weight: 700; font-size: 12pt; background: #bdd7ee; }}
      .footer {{ text-align: center; margin-top: 22pt; }}
    </style></head><body>
      <h1>KARLCON — Guest Invoice</h1>
      <div class="meta">Invoice #{esc(invoice['id'][:8].upper())}</div>
      {img}<table>{table}</table>
      <p class="footer">Thank you for booking with KARLCON Elite Retreats.</p>
    </body></html>"""


def render_pdf_with_chromium(invoice: dict, pdf_path: Path, photo: BytesIO | None = None) -> Path:
    """Prints the HTML invoice to an A4 PDF with Playwright's Chromium."""
    from playwright.sync_api import sync_playwright
    exe = os.environ.get("INVOICE_CHROMIUM_PATH")
    with sync_playwright() as pw:
        kwargs = {"executable_path": exe} if exe else {}
        browser = pw.chromium.launch(headless=True, **kwargs)
        try:
            page = browser.new_page()
            page.set_content(build_invoice_html(invoice, photo), wait_until="load")
            page.pdf(path=str(pdf_path), format="A4", print_background=True,
                     margin={"top": "18mm", "bottom": "18mm", "left": "16mm", "right": "16mm"})
        finally:
            browser.close()
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        raise RuntimeError("Chromium produced no PDF")
    return pdf_path


def make_invoice_pdf(invoice: dict, tmp_dir: Path) -> tuple[Path, str]:
    """Builds the invoice PDF with the first engine that works. Returns the
    PDF path and the engine used; raises only if every engine failed."""
    forced = os.environ.get("INVOICE_PDF_ENGINE", "").strip().lower()
    engines = [forced] if forced else ["libreoffice", "word", "chromium"]
    photo = _fetch_listing_photo(invoice)
    docx_path = tmp_dir / f"invoice_{invoice['id']}.docx"
    errors = []
    for engine in engines:
        try:
            if engine == "chromium":
                return render_pdf_with_chromium(invoice, tmp_dir / f"invoice_{invoice['id']}.pdf", photo), engine
            if engine == "libreoffice" and not find_soffice():
                raise RuntimeError("not installed")
            if engine == "word" and os.name != "nt":
                raise RuntimeError("Windows only")
            if not docx_path.exists():
                build_invoice_docx(invoice, docx_path, photo=photo)
            convert = convert_docx_to_pdf if engine == "libreoffice" else convert_docx_with_word
            return convert(docx_path, tmp_dir), engine
        except Exception as e:
            errors.append(f"{engine}: {str(e).splitlines()[0][:200] if str(e) else type(e).__name__}")
    raise RuntimeError("couldn't make the PDF — " + "; ".join(errors))


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
        try:
            pdf_path, engine = make_invoice_pdf(invoice, tmp_dir)
            upload_pdf(invoice_id, pdf_path)
            print(f"[invoice_worker] ✓ {invoice_id} rendered with {engine} and uploaded")
        except Exception as e:
            print(f"[invoice_worker] ✗ {invoice_id} failed: {e}")
            mark_error(invoice_id, str(e))


def main() -> None:
    print_active_config()
    soffice = find_soffice()
    print(f"[invoice_worker] polling {KARLON_URL} every {POLL_INTERVAL_SEC}s · PDF engine: "
          + (os.environ.get("INVOICE_PDF_ENGINE") or
             (f"LibreOffice ({soffice})" if soffice else
              "Microsoft Word if installed, else Chromium" if os.name == "nt" else "Chromium")))
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
