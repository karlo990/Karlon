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


def test_ten_per_city_new_first_then_stale_and_fresh_ones_left(monkeypatch, tmp_path):
    import time
    cp = a.Checkpoint()
    urls = [f"https://www.airbnb.com/rooms/{i}" for i in range(30)]
    now = time.time()
    for u in urls[:20]:                                   # 20 already scraped...
        cp.mark_scraped(u)
    cp.scraped_at[urls[0]] = now - 13 * 3600              # ...two of them 13 h ago (stale)
    cp.scraped_at[urls[1]] = now - 20 * 3600
    picked = a.pick_listings_to_scrape(urls, cp, 10)
    assert picked == urls[20:30]                          # the 10 new ones fill the quota
    picked = a.pick_listings_to_scrape(urls[:25], cp, 10)
    assert picked == urls[20:25] + [urls[1], urls[0]]     # 5 new, then stalest first
    assert all(not cp.is_fresh(u) or u in urls[20:] for u in picked)

    old = a.Checkpoint(scraped_urls={urls[3]})            # checkpoint from before scraped_at
    assert not old.is_fresh(urls[3])                      # refreshed once (re-pushed after a wipe)
    assert a.pick_listings_to_scrape([urls[3], urls[29]], old, 10) == [urls[29], urls[3]]

    monkeypatch.setattr(a, "CHECKPOINT_FILE", tmp_path / "cp.json")
    cp.save()
    again = a.Checkpoint.load()
    assert again.is_fresh(urls[5]) and not again.is_fresh(urls[1])


def test_all_cities_enabled_with_sane_limits():
    assert len(a.ZIMBABWE_CITIES) >= 10 and "Harare" in a.ZIMBABWE_CITIES
    assert a.MAX_LISTINGS_PER_SEARCH == 10
    assert a.MAX_SECONDS_PER_CITY <= 3600 and a.MAX_SEARCH_WORKERS <= 5
