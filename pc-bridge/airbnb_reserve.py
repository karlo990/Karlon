"""airbnb_reserve.py — book a listing on Airbnb for a reservation the app queued.

Runs inside airbnb_parallel_system.py (it reuses that process's logged-in
Airbnb browser session) via reservation_poll_loop(): every few seconds it asks
the server for ONE pending reservation (routers/reservations.py), books it,
and reports the outcome. The server then WhatsApps the guest when the request
went through.

The steps are the ones done by hand on Airbnb:
  1. open the listing with the dates and guests,
  2. scroll to and click "Reserve" (falls back to the booking page URL),
  3. on "Request to book" / "Confirm and pay": check that a saved card is
     there, read the total, compare it with the price that was quoted,
  4. write the message to the host,
  5. click "Request to book" / "Confirm and pay",
  6. wait for the trip page / confirmation.

Money safety
  * TEST MODE BY DEFAULT. Unless AIRBNB_RESERVE_LIVE=1 (local_config.py),
    everything runs except the final click and the result is 'dry_run'.
  * Price guard: stops if Airbnb's total is more than 10% (and at least $25)
    above the quote, or above AIRBNB_RESERVE_MAX_TOTAL_USD when set.
  * Before the final click, any problem -> 'failed' (nothing submitted, safe
    to retry from the app). After it, anything short of a clear confirmation
    -> 'unknown' (check Airbnb Trips; the server never re-runs it, so nothing
    is ever booked twice).
  * A screenshot of the last step of every attempt is saved under
    airbnb_data/reservations/.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

AIRBNB = "https://www.airbnb.com"

_TOTAL_RE = re.compile(r"\bTotal\b[^\n$]{0,24}\n?\s*(?:US)?\$\s?([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)
_CARD_RE = re.compile(r"(debit|credit|visa|mastercard|master card|amex|american express|discover|card)"
                      r"[^\n\d]{0,16}\d{4}\b|\bpaypal\b[^\n]{0,30}@", re.IGNORECASE)
_NOT_AVAILABLE_RE = re.compile(r"(those dates are not available|dates? (are|is) (no longer|not) available|"
                               r"isn'?t available for (those|these) dates|unavailable for your dates)", re.IGNORECASE)
_CONFIRMED_RE = re.compile(r"(request (has been )?sent|you'?re going to|reservation (is )?confirmed|"
                           r"has 24 hours to (respond|accept|confirm)|booking confirmed)", re.IGNORECASE)
_VERIFY_RE = re.compile(r"(verify|confirm) (it'?s you|your identity|your card|this payment)|"
                        r"3-?d ?secure|authenticat", re.IGNORECASE)
_ERROR_RE = re.compile(r"(payment (was |has been )?declined|card (was |has been )?declined|"
                       r"couldn'?t (complete|process)|something went wrong|please try again)", re.IGNORECASE)
_SUBMIT_NAMES = [
    re.compile(r"^\s*request to book\s*$", re.IGNORECASE),
    re.compile(r"^\s*confirm and pay\s*$", re.IGNORECASE),
    re.compile(r"^\s*(reserve|book now|confirm)\s*$", re.IGNORECASE),
]


def parse_booking_total(text: str) -> Optional[float]:
    """The stay total from the booking page ('Total USD  $421.95'). The price
    details list comes after "Price details" when present."""
    text = text or ""
    scope = text[text.lower().find("price details"):] if "price details" in text.lower() else text
    m = _TOTAL_RE.search(scope) or _TOTAL_RE.search(text)
    return float(m.group(1).replace(",", "")) if m else None


def has_saved_card(text: str) -> bool:
    """A payment method is already on the account ('Debit 8275', 'Visa •••• 1234')."""
    return bool(_CARD_RE.search(text or ""))


def price_problem(total: float, expected: Optional[float], max_total: Optional[float],
                  tolerance: float = 0.10, slack: float = 25.0) -> Optional[str]:
    """Why this total must not be paid, or None. Lower than quoted is fine."""
    if max_total and total > max_total:
        return f"total ${total:,.2f} is above the limit of ${max_total:,.2f} (AIRBNB_RESERVE_MAX_TOTAL_USD)"
    if expected and total - expected > max(expected * tolerance, slack):
        return f"price changed: Airbnb now asks ${total:,.2f}, quoted ${expected:,.2f}"
    return None


def default_host_message(job: dict) -> str:
    def fmt(d: str) -> str:
        try:
            return datetime.strptime(d, "%Y-%m-%d").strftime("%d %B %Y")
        except (TypeError, ValueError):
            return d or ""
    g = int(job.get("guests") or 1)
    return (f"Hi, I would like to book your place for {g} guest{'s' if g != 1 else ''} "
            f"from {fmt(job.get('check_in'))} to {fmt(job.get('check_out'))}. "
            f"Looking forward to the stay, thank you!")


async def _page_text(page) -> str:
    try:
        return await page.locator("body").inner_text(timeout=8000)
    except Exception:
        return ""


async def _first_visible(locators):
    for loc in locators:
        try:
            n = await loc.count()
        except Exception:
            continue
        for i in range(min(n, 6)):
            el = loc.nth(i)
            try:
                if await el.is_visible() and await el.is_enabled():
                    return el
            except Exception:
                continue
    return None


_POPUP_NAME = re.compile(r"^\s*(ok|okay|got it|accept all|accept|close|dismiss|no thanks)\s*$", re.IGNORECASE)
_NEVER_CLICK = re.compile(r"book|pay|reserve|confirm|request", re.IGNORECASE)


async def _dismiss_popups(page) -> None:
    """Close cookie banners / translation and tip dialogs. Names must match
    EXACTLY: a substring match ('has-text("OK")') also hits 'Request to bOOK'
    and would submit the booking. Anything that looks like a booking button is
    refused outright."""
    try:
        buttons = page.get_by_role("button", name=_POPUP_NAME)
        for i in range(min(await buttons.count(), 4)):
            btn = buttons.nth(i)
            try:
                label = " ".join(filter(None, [await btn.inner_text(timeout=1000),
                                               await btn.get_attribute("aria-label")]))
                if _NEVER_CLICK.search(label) or not await btn.is_visible():
                    continue
                await btn.click(timeout=2000)
                await page.wait_for_timeout(300)
            except Exception:
                continue
    except Exception:
        pass


async def _click_reserve(page) -> bool:
    """Scroll to and click the listing's Reserve button; True once on /book/."""
    btn = await _first_visible([
        page.get_by_role("button", name=re.compile(r"^\s*reserve\s*$", re.IGNORECASE)),
        page.locator('[data-testid="homes-pdp-cta-btn"]'),
        page.locator('button:has-text("Reserve")'),
    ])
    if btn is None:
        return False
    try:
        await btn.scroll_into_view_if_needed(timeout=5000)
        await btn.click(timeout=10000)
        await page.wait_for_url(re.compile(r"/book/"), timeout=30000)
        return True
    except Exception:
        return False


async def _write_host_message(page, text: str) -> bool:
    box = await _first_visible([page.locator("textarea")])
    if box is None:
        return False
    await box.fill(text)
    return True


async def _find_submit(page):
    for rx in _SUBMIT_NAMES:
        el = await _first_visible([page.get_by_role("button", name=rx)])
        if el is not None:
            return el
    return None


def _new_matches(rx, text: str, before: set) -> list:
    """Phrases that appeared only after the final click (the booking page
    itself may already say 'please try again' or similar)."""
    return [m.group(0) for m in rx.finditer(text) if m.group(0).lower() not in before]


def _phrases(text: str) -> set:
    return {m.group(0).lower() for rx in (_CONFIRMED_RE, _VERIFY_RE, _ERROR_RE) for m in rx.finditer(text)}


async def _wait_outcome(page, before_text: str = "", timeout_s: float = 90) -> dict:
    """After the final click: a confirmation, an error, or (on timeout) unknown."""
    before = _phrases(before_text)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        url = page.url
        text = await _page_text(page)
        if "/trips/" in url or "/reservation" in url or _new_matches(_CONFIRMED_RE, text, before):
            return {"status": "requested", "trip_url": url}
        if _new_matches(_VERIFY_RE, text, before):
            return {"status": "unknown", "error_message": "Airbnb is asking to verify the payment — finish it "
                                                          "in the scraper's browser window, then check Trips"}
        err = _new_matches(_ERROR_RE, text, before)
        if err:
            return {"status": "unknown", "error_message": f"Airbnb showed: '{err[0]}' after submitting — check Trips"}
        await asyncio.sleep(1.5)
    return {"status": "unknown", "error_message": f"no confirmation seen within {timeout_s:.0f} s — check Airbnb Trips"}


async def make_reservation(context, job: dict, *, live: bool, max_total_usd: Optional[float] = None,
                           shots_dir: Optional[Path] = None, base: str = AIRBNB) -> dict:
    """Book one reservation job. Returns the body for /reservations/{id}/complete."""
    rid, aid = job["id"], job["airbnb_id"]
    ci, co, guests = job["check_in"], job["check_out"], int(job.get("guests") or 1)
    result = {"status": "failed", "total_usd": None, "trip_url": None,
              "error_message": None, "cancellation_policy": None}
    stage, submitted = "open listing", False
    page = await context.new_page()

    def fail(msg: str) -> dict:
        result.update(status="failed", error_message=msg)
        return result

    try:
        await page.goto(f"{base}/rooms/{aid}?check_in={ci}&check_out={co}&adults={guests}&guests={guests}",
                        wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(1500)
        await _dismiss_popups(page)

        stage = "reserve button"
        if not await _click_reserve(page):
            await page.goto(f"{base}/book/stays/{aid}?checkin={ci}&checkout={co}"
                            f"&numberOfAdults={guests}&guestCurrency=USD",
                            wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(2000)
        await _dismiss_popups(page)

        stage = "booking page"
        text = await _page_text(page)
        if _NOT_AVAILABLE_RE.search(text):
            return fail("those dates are no longer available on Airbnb")
        if not has_saved_card(text):
            return fail("no saved payment method found on the booking page")
        total = parse_booking_total(text)
        if total is None:
            return fail("couldn't read the total on the booking page")
        result["total_usd"] = total
        if "non-refundable" in text.lower():
            result["cancellation_policy"] = "non-refundable"
        problem = price_problem(total, job.get("expected_total_usd"), max_total_usd)
        if problem:
            return fail(problem)

        stage = "message to host"
        await _write_host_message(page, (job.get("message_to_host") or "").strip() or default_host_message(job))
        submit = await _find_submit(page)
        if submit is None:
            return fail("couldn't find the Request to book / Confirm and pay button")

        if not live:
            if "/book/" not in page.url:
                # Nothing here should ever submit — but if the page moved on, say so.
                result.update(status="unknown", trip_url=page.url,
                              error_message="test mode, but the booking page moved on by itself — check Trips")
                return result
            result.update(status="dry_run",
                          error_message=f"test mode: everything checked (total ${total:,.2f}), stopped before "
                                        f"'Request to book'. Set AIRBNB_RESERVE_LIVE=1 to book for real.")
            return result

        before_text = await _page_text(page)
        stage = "submit"
        submitted = True
        await submit.scroll_into_view_if_needed(timeout=5000)
        await submit.click(timeout=15000)
        result.update(await _wait_outcome(page, before_text))
        return result
    except Exception as e:
        msg = f"{stage}: {str(e).splitlines()[0][:200]}"
        if submitted:
            result.update(status="unknown", error_message=f"after submitting — check Airbnb Trips ({msg})")
        else:
            result.update(status="failed", error_message=msg)
        return result
    finally:
        if shots_dir is not None:
            try:
                shots_dir.mkdir(parents=True, exist_ok=True)
                safe = re.sub(r"[^a-z0-9]+", "_", stage.lower())
                await page.screenshot(path=str(shots_dir / f"reservation_{rid}_{safe}.png"), full_page=True)
            except Exception:
                pass
        try:
            await page.close()
        except Exception:
            pass
