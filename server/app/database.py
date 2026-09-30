"""database.py — SQLite connection + schema + idempotent migrations."""

import sqlite3

from .config import DB_PATH


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """
    Idempotent column additions — safe to run against an existing karlon.db.
    SQLite raises OperationalError if a column already exists, so we swallow
    that and move on.  Add new entries here as the schema grows; never remove
    existing ones (that would require a full table rebuild and is out of scope).
    """
    new_cols = [
        # chats: raw WhatsApp name used to locate the chat row in WA Web DOM
        "ALTER TABLE chats    ADD COLUMN wa_name   TEXT",
        # messages: who originated the message (we send 'out', WA sends 'in')
        "ALTER TABLE messages ADD COLUMN direction TEXT DEFAULT 'in'",
        # messages: WhatsApp delivery state for outbound messages
        #   pending  → queued, not yet sent to WhatsApp
        #   sent     → successfully delivered via DOM automation
        #   error    → gave up after max retries
        "ALTER TABLE messages ADD COLUMN wa_status TEXT",
        # house_listings: which check-in/check-out window the listing was
        # scraped/priced for, so the app can search by exact dates + city
        # instead of just whatever was scraped most recently.
        "ALTER TABLE house_listings ADD COLUMN check_in  TEXT",
        "ALTER TABLE house_listings ADD COLUMN check_out TEXT",
        # house_listings: full price breakdown (audit: "store both ZAR and
        # USD"). price_zar_per_night is what Airbnb quoted, fx_rate_zar_per_usd
        # is the live rate the scraper divided by, price_usd_per_night is the
        # derived figure used everywhere in the app + invoice.
        "ALTER TABLE house_listings ADD COLUMN listing_id            TEXT",
        "ALTER TABLE house_listings ADD COLUMN price_currency        TEXT",
        "ALTER TABLE house_listings ADD COLUMN price_zar_per_night   REAL",
        "ALTER TABLE house_listings ADD COLUMN fx_rate_zar_per_usd   REAL",
        "ALTER TABLE house_listings ADD COLUMN nights                INTEGER",
        # house_listings: server-hosted copies of the scraped photos
        # (/static/houses/<key>/N.jpg) so WhatsApp delivery never depends on
        # Airbnb's CDN and the image assets live with the record.
        "ALTER TABLE house_listings ADD COLUMN images_local          TEXT",
        "ALTER TABLE house_listings ADD COLUMN first_seen_at         TEXT",
        # invoices: hard link back to the listing/offer the invoice was
        # generated from (feature: listing metadata -> invoice auto-population).
        "ALTER TABLE invoices ADD COLUMN listing_url          TEXT",
        "ALTER TABLE invoices ADD COLUMN listing_offer_id     TEXT",
        "ALTER TABLE invoices ADD COLUMN listing_title        TEXT",
        "ALTER TABLE invoices ADD COLUMN listing_images       TEXT",
        "ALTER TABLE invoices ADD COLUMN price_zar_per_night  REAL",
        "ALTER TABLE invoices ADD COLUMN fx_rate_zar_per_usd  REAL",
        # refresh_jobs: error text + how many listings landed while running
        "ALTER TABLE refresh_jobs ADD COLUMN error_message TEXT",
        "ALTER TABLE refresh_jobs ADD COLUMN started_at    TEXT",
        # messages: WhatsApp's own message id (the DOM data-id), so a
        # re-scanned message is matched even if its text/key changed.
        "ALTER TABLE messages ADD COLUMN wa_msg_id TEXT",
    ]
    for stmt in new_cols:
        try:
            conn.execute(stmt)
        except Exception:
            pass  # column already exists — harmless
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_wa_msg_id ON messages(chat_id, wa_msg_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_created ON messages(chat_id, created_at)")
    conn.commit()


def init_db() -> None:
    """Create tables if they don't exist yet, then run column migrations."""
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS chats (
            id              TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            avatar_emoji    TEXT DEFAULT '01',
            created_at      TEXT NOT NULL,
            profile_pic_url TEXT,
            wa_name         TEXT
        );

        CREATE TABLE IF NOT EXISTS messages (
            id           TEXT PRIMARY KEY,
            chat_id      TEXT NOT NULL,
            sender       TEXT NOT NULL,
            kind         TEXT NOT NULL DEFAULT 'text',
            text         TEXT,
            poll_id      TEXT,
            created_at   TEXT NOT NULL,
            external_key TEXT,
            media_url    TEXT,
            media_type   TEXT,
            direction    TEXT DEFAULT 'in',
            wa_status    TEXT,
            FOREIGN KEY (chat_id) REFERENCES chats(id)
        );

        CREATE TABLE IF NOT EXISTS polls (
            id         TEXT PRIMARY KEY,
            chat_id    TEXT NOT NULL,
            question   TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS poll_options (
            id      TEXT PRIMARY KEY,
            poll_id TEXT NOT NULL,
            text    TEXT NOT NULL,
            FOREIGN KEY (poll_id) REFERENCES polls(id)
        );

        CREATE TABLE IF NOT EXISTS poll_votes (
            poll_id   TEXT NOT NULL,
            option_id TEXT NOT NULL,
            voter_id  TEXT NOT NULL,
            PRIMARY KEY (poll_id, voter_id)
        );

        CREATE TABLE IF NOT EXISTS team_locations (
            member_id   TEXT PRIMARY KEY,
            member_name TEXT NOT NULL,
            latitude    REAL NOT NULL,
            longitude   REAL NOT NULL,
            accuracy_m  REAL,
            updated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS house_listings (
            id                   TEXT PRIMARY KEY,   -- the listing URL — stable per property
            url                  TEXT NOT NULL,
            title                TEXT,
            location             TEXT NOT NULL,       -- lowercased city, e.g. 'harare'
            check_in             TEXT,                -- YYYY-MM-DD the listing was scraped/priced for
            check_out            TEXT,                -- YYYY-MM-DD
            price_raw            TEXT,
            price_usd_per_night  REAL,
            images               TEXT,                -- JSON array of image URLs
            lat                  REAL,
            lng                  REAL,
            updated_at           TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS listing_offers (
            -- One row per (listing, check_in, check_out) the scraper priced.
            -- house_listings is the *property* (title, photos, coords) and
            -- is upserted by URL; this table is the *price for a date window*
            -- and is never overwritten by a scrape for different dates, so a
            -- (city, check_in, check_out) search keeps returning what was
            -- scraped for it until check_out has passed (see purge in
            -- routers/houses.py). id = sha1(url|check_in|check_out).
            id                    TEXT PRIMARY KEY,
            listing_url           TEXT NOT NULL,
            location              TEXT NOT NULL,
            check_in              TEXT NOT NULL,
            check_out             TEXT NOT NULL,
            nights                INTEGER,
            price_raw             TEXT,
            price_currency        TEXT,
            price_zar_per_night   REAL,
            price_usd_per_night   REAL,
            fx_rate_zar_per_usd   REAL,
            refresh_job_id        INTEGER,
            scraped_at            TEXT NOT NULL,
            FOREIGN KEY (listing_url) REFERENCES house_listings(id)
        );

        CREATE TABLE IF NOT EXISTS refresh_jobs (
            -- On-demand Airbnb re-price jobs. Queued by the Android app's
            -- house popup (POST /api/houses/refresh) when an exact
            -- (location, check_in, check_out) search on house_listings
            -- comes back empty; drained by airbnb_parallel_system.py's
            -- ondemand_poll_loop(), which polls .../refresh/pending every
            -- ~20s and POSTs back to .../refresh/{id}/complete.
            -- id is INTEGER (not the TEXT ids used elsewhere in this file)
            -- because the Android HouseRefreshResponse DTO declares `id: Int`.
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            location     TEXT NOT NULL,
            check_in     TEXT NOT NULL,          -- YYYY-MM-DD
            check_out    TEXT NOT NULL,          -- YYYY-MM-DD
            status       TEXT NOT NULL DEFAULT 'pending',  -- pending | in_progress | done
            pushed_count INTEGER,
            created_at   TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS invoices (
            id           TEXT PRIMARY KEY,
            chat_id      TEXT,
            guest_name   TEXT NOT NULL,
            id_number    TEXT NOT NULL,
            location     TEXT NOT NULL,
            property_name TEXT NOT NULL,
            check_in     TEXT,
            check_out    TEXT,
            nights       INTEGER DEFAULT 1,
            rate         REAL NOT NULL DEFAULT 0,
            total        REAL NOT NULL DEFAULT 0,
            currency     TEXT DEFAULT 'USD',
            status       TEXT NOT NULL DEFAULT 'pending',
            pdf_url      TEXT,
            error_message TEXT,
            created_by   TEXT,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            FOREIGN KEY (chat_id) REFERENCES chats(id)
        );
        """
    )
    # Fast dedup lookup for wa_bridge imports (routers/messages.py import_messages).
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_chat_extkey "
        "ON messages(chat_id, external_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_house_listings_location "
        "ON house_listings(location, updated_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_refresh_jobs_status "
        "ON refresh_jobs(status, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_listing_offers_search "
        "ON listing_offers(location, check_in, check_out, scraped_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_listing_offers_url "
        "ON listing_offers(listing_url)"
    )
    conn.commit()
    # Run column migrations on existing databases
    _migrate(conn)
    conn.close()
