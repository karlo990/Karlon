"""Price per night, capacity and suburb from Airbnb listing text."""
import airbnb_parallel_system as a


def test_per_night_is_total_divided_by_nights(monkeypatch):
    monkeypatch.setattr(a, "CURRENT_ZAR_TO_USD_RATE", 16.41)
    cases = [
        ("$3,172 for 22 nights", None, 144.18, 22, 3172.0),     # full line
        ("$3,172", 22, 144.18, 22, 3172.0),                     # amount only: stay we asked for
        ("$3,500 $3,172 for 22 nights", None, 144.18, 22, 3172.0),  # struck-through original
        ("$144 night", 22, 144.0, 22, 3168.0),                  # already per night
        ("R2,600 for 2 nights", None, 79.22, 2, 2600.0),        # ZAR -> USD per night
    ]
    for raw, expected_nights, usd_per_night, nights, total in cases:
        d = a.parse_price_text(raw, expected_nights)
        assert (d["usd_per_night"], d["nights"], d["amount"]) == (usd_per_night, nights, total), raw
    assert a.parse_price_text("R1,200 - R1,500")["per_night"] == 1350.0     # range: midpoint


def test_capacity_and_suburb_from_the_listing_page():
    page = ("8 @Metcalf One\nEntire rental unit in Harare, Zimbabwe\n"
            "6 guests · 3 bedrooms · 3 beds · 2.5 baths\n★ 4.67 · 3 reviews\nOutdoor entertainment\n"
            "The pool, bbq area, and outdoor seating are great for summer trips.\n"
            "Discover your ideal retreat in a charming Greendale apartment for travellers exploring Harare.\n"
            "What this place offers\nWifi\nWhere you’ll be\nHarare\nMore places to stay in Borrowdale\n")
    assert a.extract_capacity(page) == "6 guests · 3 bedrooms · 3 beds · 2.5 baths"
    overview = a.listing_overview(page)
    assert a.find_suburb("", overview) == "Greendale"
    assert "Borrowdale" not in overview                       # below the fold: ignored
    assert a.find_suburb("Close to Borrowdale Brooke estate") == "Borrowdale Brooke"
