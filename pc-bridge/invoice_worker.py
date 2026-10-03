"""invoice_worker.py — renders pending Karlon invoices to PDF.

Runs as a separate, always-on process on the same PC as wa_bridge.py
(same LAN as the Karlon server, or pointed at the HF Space via
KARLON_URL — same env var wa_bridge.py uses). It builds the invoice and
turns it into a PDF with the first of these that works on this PC:
  1. Chromium (already installed for wa_bridge.py via Playwright), printing
     the KARLCON invoice layout (KARLCON_Invoice_KCER-2026-0010.pdf)
  2. LibreOffice (soffice), converting a plainer .docx built with python-docx
  3. Microsoft Word, converting the same .docx (driven via PowerShell)
WhatsApp delivery of
the finished PDF is handled entirely by wa_bridge.py's existing outbox
poller — see routers/invoices.py's upload_invoice_pdf() for how the
hand-off happens (it inserts a normal outbox message once the PDF is
uploaded here).

REQUIRES:
    pip install python-docx requests playwright
    Chromium (installed with Playwright: `playwright install chromium`) prints
    the KARLCON invoice layout. LibreOffice / Microsoft Word are only a
    fallback. Force one with INVOICE_PDF_ENGINE=chromium|libreoffice|word.
    Check it on this PC:  python invoice_worker.py --sample

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


# Fixed parts of the invoice (same as KARLCON_Invoice_KCER-2026-0010.pdf).
COMPANY_LINES = ["KARLCON CONSULTANCY", "(PRIVATE) LIMITED", "Reg. No. 15017/2023", "Harare, Zimbabwe"]
COMPANY_SITE = "karl-con-elite-retreats.vercel.app"
CHECK_IN_TIME, CHECK_OUT_TIME = "2:00 PM", "10:00 AM"
SERVICE_FEE_PCT = float(os.environ.get("INVOICE_SERVICE_FEE_PCT", "15"))
PAYMENT_METHODS = [
    "ZAR / USD Cash (in-person or mobile transfer)",
    "EcoCash — USD Merchant Wallet (code shared via WhatsApp)",
    "ZIPIT / Innbucks (USD equivalent)",
    "Bank Transfer — FBC / CBZ (USD)",
]
NOTES = ("Full payment of the Booking Amount is due within 48 hours of this invoice being issued. "
         "Unpaid invoices past this window will release the property hold automatically, per the "
         "KARLCON Elite Retreats Booking Agent Terms & Conditions.")


def _fmt_long(d: str | None) -> str:
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(d)[:10]).strftime("%d %B %Y").lstrip("0")
    except (TypeError, ValueError):
        return "-"


def _fmt_short(d: str | None) -> str:
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(d)[:10]).strftime("%d %b %Y").lstrip("0")
    except (TypeError, ValueError):
        return "-"


def invoice_figures(invoice: dict) -> dict:
    """Subtotal, service fee, total and balance, as printed."""
    nights = int(invoice.get("nights") or 1)
    rate = float(invoice.get("rate") or 0)
    subtotal = round(rate * nights, 2)
    fee = round(subtotal * SERVICE_FEE_PCT / 100, 2)
    total = round(subtotal + fee, 2)
    paid = float(invoice.get("amount_paid") or 0)
    return {"nights": nights, "rate": rate, "subtotal": subtotal, "fee": fee, "total": total,
            "paid": paid, "balance": round(total - paid, 2)}


def _listing_details(invoice: dict) -> dict:
    """Suburb and size of the linked listing, from the server (best effort)."""
    url = invoice.get("listing_url")
    if not url:
        return {}
    try:
        r = get_session().get(f"{KARLON_URL}/api/houses/listing", params={"url": url}, timeout=HTTP_TIMEOUT)
        return r.json() if r.ok else {}
    except Exception:
        return {}


def build_invoice_html(invoice: dict, photo: BytesIO | None = None, listing: dict | None = None) -> str:
    """The invoice as a printable A4 page, laid out like KARLCON_Invoice_KCER-2026-0010.pdf."""
    esc = lambda v: html.escape(str(v if v not in (None, "") else "-"))  # noqa: E731
    money = lambda v: f"{v:,.2f}"  # noqa: E731
    listing = listing or {}
    cur = invoice.get("currency") or "USD"
    fig = invoice_figures(invoice)
    number = invoice.get("invoice_number") or f"KCER-{str(invoice.get('created_at') or '')[:4]}-{invoice['id'][:6].upper()}"
    issue = _fmt_long(invoice.get("created_at"))
    guests = int(invoice.get("guests") or 1)
    prop = invoice.get("property_name") or listing.get("ref_code") or "Property"
    place = ", ".join(x for x in [listing.get("neighbourhood") or "",
                                  (invoice.get("location") or "").title()] if x) or (invoice.get("location") or "").title()
    if listing.get("neighbourhood") and (invoice.get("location") or "").lower() in listing["neighbourhood"].lower():
        place = listing["neighbourhood"]
    size = listing.get("capacity") or ""
    detail = " · ".join(x for x in [place, size] if x)
    paid_badge = "PAID" if fig["balance"] <= 0 and fig["total"] > 0 else "PAYMENT DUE"
    payments = "".join(f"<li>{esc(m)}</li>" for m in PAYMENT_METHODS)
    img = ""
    if photo is not None:
        data = photo.getvalue()
        mime = "image/png" if data[:4] == b"\x89PNG" else "image/webp" if data[8:12] == b"WEBP" else "image/jpeg"
        img = f'<img class="photo" src="data:{mime};base64,{base64.b64encode(data).decode()}">'
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
  @page {{ size: A4; margin: 16mm 14mm 14mm; }}
  body {{ font-family: Helvetica, Arial, sans-serif; color: #222; font-size: 9.5pt; line-height: 1.45; margin: 0; }}
  .top {{ display: flex; justify-content: space-between; align-items: flex-start; }}
  .brand h1 {{ margin: 0; font-size: 28pt; color: #2f4a3a; letter-spacing: .5pt; }}
  .brand div {{ letter-spacing: 3.5pt; font-size: 8pt; color: #666; margin-top: 2pt; }}
  .inv {{ text-align: right; }}
  .inv h2 {{ margin: 0; font-size: 20pt; }}
  .inv .no {{ color: #555; margin: 3pt 0 6pt; }}
  .badge {{ display: inline-block; background: #c9973f; color: #fff; font-weight: 700; font-size: 7pt;
            padding: 3pt 0; width: 120pt; text-align: center; border-radius: 2pt; }}
  hr {{ border: 0; border-top: 1px solid #ccc; margin: 14pt 0; }}
  .label {{ font-size: 7.5pt; font-weight: 700; color: #666; letter-spacing: .3pt; margin-bottom: 5pt; }}
  .cols {{ display: flex; }} .cols > div {{ flex: 1; }}
  .strong {{ font-weight: 700; font-size: 10pt; }}
  .muted {{ color: #666; }} a {{ color: #1a5fb4; text-decoration: none; }}
  .details td {{ padding: 1pt 0; }} .details td:first-child {{ color: #666; width: 70pt; }}
  .details td:last-child {{ font-weight: 700; }}
  .stay {{ background: #efe0c8; padding: 10pt 6pt; display: flex; }}
  .stay .p {{ flex: 2.2; }} .stay .c {{ flex: 1; }} .stay .g {{ flex: .6; text-align: right; }}
  .stay .label {{ color: #2f4a3a; }}
  .stay .t {{ color: #c9973f; font-weight: 700; }}
  .small {{ font-size: 8pt; color: #666; }}
  table.items {{ width: 100%; border-collapse: collapse; margin-top: 4pt; }}
  table.items th {{ background: #2f4a3a; color: #fff; font-size: 7.5pt; text-align: left; padding: 6pt; }}
  table.items td {{ padding: 6pt; vertical-align: top; }}
  table.items .n {{ text-align: center; }} table.items .r {{ text-align: right; }}
  table.items tr.last td {{ border-bottom: 1px solid #ccc; }}
  .totals {{ width: 46%; margin: 10pt 0 0 auto; border-collapse: collapse; }}
  .totals td {{ padding: 6pt; }} .totals td:last-child {{ text-align: right; }}
  .totals .gap td {{ padding-top: 14pt; }}
  .totals .hl td {{ background: #efe0c8; font-weight: 700; font-size: 10pt; }}
  .totals .muted td:first-child {{ color: #666; }}
  ul {{ margin: 0; padding-left: 0; list-style: none; }} li:before {{ content: "• "; }}
  .photo {{ display: block; max-width: 100%; max-height: 150pt; margin: 0 0 10pt; border-radius: 3pt; }}
  .foot {{ position: fixed; bottom: 0; left: 0; right: 0; text-align: center; font-size: 7pt; color: #666; }}
</style></head><body>
  <div class="top">
    <div class="brand"><h1>KARLCON</h1><div>ELITE RETREATS</div></div>
    <div class="inv"><h2>INVOICE</h2><div class="no">No. {esc(number)}</div><span class="badge">{paid_badge}</span></div>
  </div>
  <hr>
  <div class="cols">
    <div><div class="label">BILLED FROM</div><div class="strong">{esc(COMPANY_LINES[0])}</div>
      {''.join(f'<div>{esc(x)}</div>' for x in COMPANY_LINES[1:])}<a>{esc(COMPANY_SITE)}</a></div>
    <div><div class="label">BILLED TO</div><div class="strong">{esc(invoice.get('guest_name'))}</div>
      <div class="muted">ID/Passport No: {esc(invoice.get('id_number'))}</div>
      <div class="muted">{guests} Guest{'s' if guests != 1 else ''}</div></div>
  </div>
  <hr>
  <div class="label">INVOICE DETAILS</div>
  <table class="details">
    <tr><td>Issue Date:</td><td>{esc(issue)}</td></tr>
    <tr><td>Due Date:</td><td>{esc(issue)}</td></tr>
    <tr><td>Booking Ref:</td><td>{esc(number)}</td></tr>
  </table>
  <hr>
  {img}
  <div class="stay">
    <div class="p"><div class="label">PROPERTY</div><div class="strong">{esc(prop)}</div>
      <div class="muted">{esc(place)}</div><div class="small">{esc(size) if size else ''}</div></div>
    <div class="c"><div class="label">CHECK-IN</div>{esc(_fmt_short(invoice.get('check_in')))}<div class="t">{CHECK_IN_TIME}</div></div>
    <div class="c"><div class="label">CHECK-OUT</div>{esc(_fmt_short(invoice.get('check_out')))}<div class="t">{CHECK_OUT_TIME}</div></div>
    <div class="g"><div class="label">GUESTS</div><span class="muted">{guests}</span></div>
  </div>
  <hr>
  <table class="items">
    <tr><th>DESCRIPTION</th><th class="n">NIGHTS</th><th class="r">RATE ({esc(cur)})</th><th class="r">AMOUNT</th></tr>
    <tr><td><div class="strong">Accommodation — {esc(prop)}</div><div class="small">{esc(detail)}</div></td>
      <td class="n">{fig['nights']}</td><td class="r">{money(fig['rate'])}</td><td class="r">{money(fig['subtotal'])}</td></tr>
    <tr><td class="strong">Additional Guest Charge —</td><td></td><td class="r">0.00</td><td class="r">0.00</td></tr>
    <tr class="last"><td class="strong">Other (specify) —</td><td></td><td class="r">0.00</td><td class="r">0.00</td></tr>
  </table>
  <table class="totals">
    <tr class="muted"><td>Subtotal</td><td>{esc(cur)} {money(fig['subtotal'])}</td></tr>
    <tr class="muted gap"><td>Service Fee ({SERVICE_FEE_PCT:g}%)</td><td>{esc(cur)} {money(fig['fee'])}</td></tr>
    <tr class="hl gap"><td>Total Booking Amount</td><td>{esc(cur)} {money(fig['total'])}</td></tr>
    <tr class="muted gap"><td>Amount Paid</td><td>{esc(cur)} {money(fig['paid'])}</td></tr>
    <tr class="hl"><td>Balance Due</td><td>{esc(cur)} {money(fig['balance'])}</td></tr>
  </table>
  <hr>
  <div class="cols">
    <div><div class="label">PAYMENT METHODS</div><ul>{payments}</ul></div>
    <div><div class="label">NOTES</div><div class="muted">{esc(NOTES)}</div></div>
  </div>
  <div class="foot">KARLCON Consultancy (Private) Limited · Reg. No. 15017/2023 · Harare, Zimbabwe</div>
</body></html>"""


def render_pdf_with_chromium(invoice: dict, pdf_path: Path, photo: BytesIO | None = None,
                             listing: dict | None = None) -> Path:
    """Prints the HTML invoice to an A4 PDF with Playwright's Chromium."""
    from playwright.sync_api import sync_playwright
    exe = os.environ.get("INVOICE_CHROMIUM_PATH")
    with sync_playwright() as pw:
        kwargs = {"executable_path": exe} if exe else {}
        browser = pw.chromium.launch(headless=True, **kwargs)
        try:
            page = browser.new_page()
            page.set_content(build_invoice_html(invoice, photo, listing), wait_until="load")
            page.pdf(path=str(pdf_path), format="A4", print_background=True, prefer_css_page_size=True)
        finally:
            browser.close()
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        raise RuntimeError("Chromium produced no PDF")
    return pdf_path


def make_invoice_pdf(invoice: dict, tmp_dir: Path) -> tuple[Path, str]:
    """Builds the invoice PDF with the first engine that works. Returns the
    PDF path and the engine used; raises only if every engine failed."""
    forced = os.environ.get("INVOICE_PDF_ENGINE", "").strip().lower()
    # Chromium first: it prints the KARLCON invoice layout exactly. The .docx
    # engines are a fallback with a plainer layout.
    engines = [forced] if forced else ["chromium", "libreoffice", "word"]
    photo = _fetch_listing_photo(invoice) if os.environ.get("INVOICE_SHOW_PHOTO") == "1" else None
    listing = _listing_details(invoice)
    number = invoice.get("invoice_number") or invoice["id"]
    docx_path = tmp_dir / f"KARLCON_Invoice_{number}.docx"
    errors = []
    for engine in engines:
        try:
            if engine == "chromium":
                return render_pdf_with_chromium(invoice, tmp_dir / f"KARLCON_Invoice_{number}.pdf", photo, listing), engine
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


SAMPLE_INVOICE = {
    "id": "sample00-0000-0000-0000-000000000000", "invoice_number": "KCER-2026-0010",
    "created_at": "2026-09-28T10:00:00+00:00", "guest_name": "Leon Pikiraih",
    "id_number": "63-2265159M22", "guests": 2, "location": "harare",
    "property_name": "Luxury Studio Apartment", "check_in": "2026-09-28", "check_out": "2026-09-29",
    "nights": 1, "rate": 90.0, "currency": "USD",
}


def write_sample(out: Path = Path("sample_invoice.pdf")) -> Path:
    """`python invoice_worker.py --sample`: renders a sample invoice locally
    (no server needed) to check the PDF engine and the layout."""
    import tempfile as _tf
    with _tf.TemporaryDirectory() as tmp:
        sample = dict(SAMPLE_INVOICE, listing_url=None)
        pdf, engine = make_invoice_pdf(sample, Path(tmp))
        shutil.copyfile(pdf, out)
    print(f"[invoice_worker] sample invoice written to {out.resolve()} using {engine}")
    return out


def main() -> None:
    if "--sample" in sys.argv:
        write_sample()
        return
    print_active_config()
    soffice = find_soffice()
    print(f"[invoice_worker] polling {KARLON_URL} every {POLL_INTERVAL_SEC}s · PDF engine: "
          + (os.environ.get("INVOICE_PDF_ENGINE") or "Chromium (KARLCON layout)")
          + (f"; fallback LibreOffice ({soffice})" if soffice else ""))
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
