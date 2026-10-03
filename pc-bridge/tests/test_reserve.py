"""airbnb_reserve.py against a fake Airbnb (listing → booking page → trip page).

The fake pages copy the shape of the real flow: a Reserve button on the
listing, a "Request to book" page with the saved card, price details and the
message box. No real Airbnb, network or money is involved."""
import asyncio
import threading
from urllib.parse import parse_qs, urlparse

import pytest

from airbnb_reserve import (default_host_message, has_saved_card, make_reservation,
                            parse_booking_total, price_problem)
from conftest import _chromium_path

BASE = "https://fake-airbnb.test"
JOB = {"id": 7, "airbnb_id": "1731941150603946314", "ref_code": "KCER 101",
       "check_in": "2026-10-05", "check_out": "2026-10-08", "nights": 3, "guests": 3,
       "expected_total_usd": 422.01, "message_to_host": None}

BOOKING_TEXT = """Request to book
Your trip
Dates  5–8 Oct
Guests  3 guests
Pay with
Debit 8275
Message the host
Price details
$140.67 x 3 nights  $422.03
Airbnb service fee  -$0.08
Total USD  $421.95
"""


def _listing_html(reserve_button=True):
    btn = ('<button data-testid="homes-pdp-cta-btn" onclick="location.href=\'/book/stays/'
           '1731941150603946314\' + location.search">Reserve</button>') if reserve_button else ""
    return f"<html><body><h1>Lovely place</h1><div style='height:1500px'></div>{btn}</body></html>"


def _booking_html(card="Debit 8275", total="$421.95", on_submit="trips", extra=""):
    submit = {
        "trips": "location.href='/trips/v1/99?msg='+encodeURIComponent(document.querySelector('textarea').value)",
        "declined": "document.getElementById('err').textContent='Your payment was declined.'",
        "nothing": "void 0",
    }[on_submit]
    return f"""<html><body>
      <h1>Request to book</h1>
      <h2>Your trip</h2>
      <div role="dialog" id="cookies">We use cookies
        <button onclick="document.getElementById('cookies').remove()">OK</button></div>
      <div>Pay with</div><div>{card}</div>
      <div>Message the host</div><textarea></textarea>
      <div>Price details</div><div>$140.67 x 3 nights $422.03</div>
      <div>Total USD {total}</div>{extra}
      <div id="err"></div>
      <button onclick="{submit}">Request to book</button>
    </body></html>"""


def _run(coro):
    """Async Playwright in its own thread (the session's sync browser owns this one)."""
    box = {}

    def target():
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as e:      # surface in the test thread
            box["error"] = e
    t = threading.Thread(target=target)
    t.start()
    t.join(180)
    if "error" in box:
        raise box["error"]
    return box["value"]


async def _book(tmp_path=None, live=True, max_total=None, job=None, **site):
    from playwright.async_api import async_playwright
    seen = []
    listing = _listing_html(site.pop("reserve_button", True))
    booking = _booking_html(**site)

    async with async_playwright() as pw:
        exe = _chromium_path()
        kwargs = {"executable_path": exe, "args": ["--no-sandbox"]} if exe else {}
        browser = await pw.chromium.launch(**kwargs)
        context = await browser.new_context()

        async def handler(route):
            url = route.request.url
            seen.append(url)
            path = urlparse(url).path
            if path.startswith("/rooms/"):
                body = listing
            elif path.startswith("/book/"):
                body = booking
            elif path.startswith("/trips/"):
                body = "<html><body><h1>Request sent</h1><p>The host has 24 hours to respond.</p></body></html>"
            else:
                body = "<html><body>not found</body></html>"
            await route.fulfill(status=200, content_type="text/html", body=body)

        await context.route(f"{BASE}/**", handler)
        result = await make_reservation(context, dict(job or JOB), live=live, max_total_usd=max_total,
                                        shots_dir=tmp_path, base=BASE)
        await browser.close()
    return result, seen


# ── pure helpers ──────────────────────────────────────────────────────────
def test_parse_booking_total_reads_the_total_not_the_nightly_line():
    assert parse_booking_total(BOOKING_TEXT) == 421.95
    assert parse_booking_total("Price details\nTotal (USD)\n$1,234.50") == 1234.50
    assert parse_booking_total("no prices here") is None


def test_saved_card_detection():
    assert has_saved_card(BOOKING_TEXT)
    assert has_saved_card("Visa •••• 1234")
    assert not has_saved_card("Pay with\nAdd a payment method")


def test_price_guard():
    assert price_problem(421.95, 422.01, None) is None            # cheaper is fine
    assert price_problem(440, 422.01, None) is None               # within $25
    assert "price changed" in price_problem(480, 422.01, None)
    assert "above the limit" in price_problem(421.95, 422.01, 400)
    assert price_problem(9999, None, None) is None                # no quote, no cap


def test_default_host_message():
    msg = default_host_message(JOB)
    assert "3 guests" in msg and "05 October 2026" in msg and "08 October 2026" in msg


# ── the browser flow ─────────────────────────────────────────────────────
def test_test_mode_checks_everything_but_does_not_submit(tmp_path):
    result, seen = _run(_book(tmp_path, live=False))
    assert result["status"] == "dry_run" and result["total_usd"] == 421.95
    assert not any("/trips/" in u for u in seen)                   # never clicked Request to book
    rooms = [u for u in seen if "/rooms/" in u][0]
    q = parse_qs(urlparse(rooms).query)
    assert (q["check_in"], q["check_out"], q["adults"]) == (["2026-10-05"], ["2026-10-08"], ["3"])
    assert any("/book/stays/" in u for u in seen)                  # got there via the Reserve button
    assert list(tmp_path.glob("reservation_7_*.png"))


def test_popup_closing_never_clicks_request_to_book(tmp_path):
    # 'OK' is a substring of 'Request to bOOK' — the cookie banner must be closed
    # by exact name and the booking must stay unsubmitted in test mode.
    result, seen = _run(_book(tmp_path, live=False))
    assert result["status"] == "dry_run"
    assert not any("/trips/" in u for u in seen)


def test_booking_page_wording_is_not_mistaken_for_a_confirmation(tmp_path):
    # The booking page already says "Your trip"; nothing happens on submit here,
    # so the outcome must not be 'requested'.
    async def go():
        return await _book(tmp_path, live=True, on_submit="nothing")
    import airbnb_reserve
    orig = airbnb_reserve._wait_outcome
    airbnb_reserve._wait_outcome = lambda page, before="", timeout_s=4: orig(page, before, timeout_s)
    try:
        result, _ = _run(go())
    finally:
        airbnb_reserve._wait_outcome = orig
    assert result["status"] == "unknown" and "no confirmation" in result["error_message"]


def test_live_mode_writes_to_host_and_requests_to_book(tmp_path):
    result, seen = _run(_book(tmp_path, live=True))
    assert result["status"] == "requested", result
    assert "/trips/" in result["trip_url"]
    trip = [u for u in seen if "/trips/" in u][0]
    msg = parse_qs(urlparse(trip).query)["msg"][0]
    assert "3 guests" in msg and "October 2026" in msg


def test_custom_message_is_sent_verbatim(tmp_path):
    job = dict(JOB, message_to_host="Hi! Arriving late, around 10pm.")
    result, seen = _run(_book(tmp_path, live=True, job=job))
    trip = [u for u in seen if "/trips/" in u][0]
    assert parse_qs(urlparse(trip).query)["msg"][0] == "Hi! Arriving late, around 10pm."


def test_falls_back_to_the_booking_url_without_a_reserve_button(tmp_path):
    result, seen = _run(_book(tmp_path, live=False, reserve_button=False))
    assert result["status"] == "dry_run"
    book = [u for u in seen if "/book/stays/" in u][0]
    q = parse_qs(urlparse(book).query)
    assert (q["checkin"], q["checkout"], q["numberOfAdults"]) == (["2026-10-05"], ["2026-10-08"], ["3"])


@pytest.mark.parametrize("site,max_total,why", [
    ({"card": "Add a payment method"}, None, "no saved payment method"),
    ({"total": "$650.00"}, None, "price changed"),
    ({}, 300, "above the limit"),
    ({"extra": "<p>Those dates are not available</p>"}, None, "no longer available"),
])
def test_stops_before_paying_when_something_is_off(tmp_path, site, max_total, why):
    result, seen = _run(_book(tmp_path, live=True, max_total=max_total, **site))
    assert result["status"] == "failed" and why in result["error_message"]
    assert not any("/trips/" in u for u in seen)


def test_error_after_submitting_is_unknown_not_failed(tmp_path):
    result, _ = _run(_book(tmp_path, live=True, on_submit="declined"))
    assert result["status"] == "unknown"                           # never auto-retried
    assert "declined" in result["error_message"]
