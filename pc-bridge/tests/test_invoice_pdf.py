"""Invoices become PDFs even on a PC without LibreOffice or Word."""
import threading

import invoice_worker as iw
from conftest import _chromium_path

INVOICE = {"id": "5dee296e-b2cc-40b1-9f50-de994c2251c5", "guest_name": "Ahmed <Test>", "id_number": "P1234567",
           "location": "harare", "property_name": "KCER 101", "check_in": "2026-10-05",
           "check_out": "2026-10-08", "nights": 3, "rate": 140.67, "total": 422.01, "currency": "USD"}


def test_html_invoice_has_every_field_and_escapes_input():
    page = iw.build_invoice_html(INVOICE)
    for text in ("Ahmed &lt;Test&gt;", "P1234567", "Harare", "KCER 101", "5 Oct 2026", "8 Oct 2026",
                 "140.67", "USD 422.01", "Service Fee (15%)", "USD 63.30", "Balance Due", "USD 485.31",
                 "PAYMENT DUE", "BILLED FROM", "2:00 PM", "10:00 AM"):
        assert text in page, text
    assert "<Test>" not in page


def test_falls_back_to_chromium_when_libreoffice_and_word_are_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(iw, "find_soffice", lambda: None)
    monkeypatch.setattr(iw, "_fetch_listing_photo", lambda inv: None)
    monkeypatch.setattr(iw.os, "name", "posix")                  # no Word either
    monkeypatch.setenv("INVOICE_CHROMIUM_PATH", _chromium_path() or "")
    box = {}
    # Own thread: the test session's browser already holds this one (on the PC
    # the worker is a separate process).
    t = threading.Thread(target=lambda: box.update(r=iw.make_invoice_pdf(INVOICE, tmp_path)))
    t.start()
    t.join(120)
    pdf, engine = box["r"]
    assert engine == "chromium"
    data = pdf.read_bytes()
    assert data[:5] == b"%PDF-" and len(data) > 1000


def test_every_engine_failing_is_reported_together(tmp_path, monkeypatch):
    monkeypatch.setattr(iw, "find_soffice", lambda: None)
    monkeypatch.setattr(iw, "_fetch_listing_photo", lambda inv: None)
    monkeypatch.setattr(iw.os, "name", "posix")
    monkeypatch.setattr(iw, "render_pdf_with_chromium", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        iw.make_invoice_pdf(INVOICE, tmp_path)
    except RuntimeError as e:
        assert "libreoffice: not installed" in str(e) and "word" in str(e) and "chromium: boom" in str(e)
    else:
        raise AssertionError("expected a failure")


def test_invoice_number_and_figures():
    page = iw.build_invoice_html(dict(INVOICE, invoice_number="KCER-2026-0010", guests=2))
    assert "No. KCER-2026-0010" in page and "2 Guests" in page
    fig = iw.invoice_figures({"nights": 1, "rate": 90})
    assert (fig["subtotal"], fig["fee"], fig["total"], fig["balance"]) == (90.0, 13.5, 103.5, 103.5)
