"""models.py — Pydantic request bodies + row serialization."""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class UpsertChatIn(BaseModel):
    id: str
    name: str
    avatar_emoji: str = "?"
    profile_pic_url: Optional[str] = None
    # Raw WhatsApp name used by wa_bridge to locate the chat row in WA Web DOM.
    # Distinct from `name` which may have a display prefix (e.g. "📱 +263…").
    wa_name: Optional[str] = None


class SendMessageIn(BaseModel):
    sender: str = "Guest"
    text: str


class BroadcastIn(BaseModel):
    sender: str = "Front Desk"
    text: str


class CreatePollIn(BaseModel):
    sender: str = "Guest"
    question: str
    options: List[str]


class VoteIn(BaseModel):
    option_id: str
    voter_id: str


class ImportMessageItem(BaseModel):
    sender: str = "Unknown"
    text: Optional[str] = ""
    created_at: Optional[str] = None
    external_key: Optional[str] = None
    media_url: Optional[str] = None
    media_type: Optional[str] = None
    # "in" | "out" | None. wa_bridge.detect_direction() computes this at
    # scrape time (aria-label="You:" / tail-out vs the contact's own name /
    # tail-in — see wa_bridge.py). Optional and defaulted to None so older
    # wa_bridge builds that don't send it yet still validate; the import
    # route falls back to sniffing `sender` when it's absent.
    direction: Optional[str] = None


class ImportMessagesIn(BaseModel):
    messages: List[ImportMessageItem]


class SnapshotChatIn(BaseModel):
    id: str
    name: str
    wa_name: Optional[str] = None
    avatar_emoji: str = "?"
    profile_pic_url: Optional[str] = None


class SnapshotMessageIn(BaseModel):
    external_key: str
    wa_id: Optional[str] = None          # WhatsApp's DOM data-id
    direction: str = "in"                # "in" | "out"
    sender: str = "Unknown"
    kind: str = "text"                   # "text" | "image"
    text: Optional[str] = ""
    created_at: str                      # ISO, UTC; screen order preserved to the ms
    seq: Optional[int] = None
    time_source: Optional[str] = None


class ChatSnapshotIn(BaseModel):
    """One chat as wa_bridge read it (routers/chat_sync.py)."""
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    schema_name: str = Field("karlon.chat_snapshot/1", alias="schema")
    chat: SnapshotChatIn
    scanned_at: Optional[str] = None
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    reached_top: bool = False
    messages: List[SnapshotMessageIn] = []
    stats: Optional[dict] = None


class ReservationCreateIn(BaseModel):
    """Body the app POSTs to /api/reservations (Reserve in the house popup)."""
    listing_id: str                            # house_listings.id (the listing URL)
    chat_id: Optional[str] = None              # who gets the "reservation made" WhatsApp
    offer_id: Optional[str] = None             # the dated offer the app was showing
    check_in: Optional[str] = None             # YYYY-MM-DD; else taken from the offer
    check_out: Optional[str] = None
    guests: int = Field(1, ge=1, le=16)
    message: Optional[str] = None              # note to the host (a default is used if blank)
    created_by: str = "Front Desk"


class ReservationCompleteIn(BaseModel):
    """Body the PC scraper POSTs to /api/reservations/{id}/complete."""
    status: str = Field(..., pattern="^(requested|failed|unknown|dry_run)$")
    total_usd: Optional[float] = None
    trip_url: Optional[str] = None
    error_message: Optional[str] = None
    cancellation_policy: Optional[str] = None


class LocationIn(BaseModel):
    member_id: str
    member_name: str
    latitude: float
    longitude: float
    accuracy_m: Optional[float] = None


class InvoiceCreateIn(BaseModel):
    guest_name: str
    id_number: str
    location: str
    property_name: str
    check_in: Optional[str] = None
    check_out: Optional[str] = None
    nights: int = 1
    rate: float = 0.0
    currency: str = "USD"
    guests: int = Field(1, ge=1, le=30)
    created_by: str = "Front Desk"
    # Optional — if set, the generated PDF is also delivered to WhatsApp
    # via this chat's outbox once invoice_worker.py finishes rendering it.
    chat_id: Optional[str] = None
    # Optional — set when the invoice was started by tapping "Book" on a
    # scraped listing. The server copies that listing's title, photos and
    # ZAR/USD/FX breakdown onto the invoice row so it stays linked to the
    # exact offer it was priced from (routers/invoices.py: create_invoice).
    listing_url: Optional[str] = None
    listing_offer_id: Optional[str] = None


class InvoiceErrorIn(BaseModel):
    error_message: str


# ── Houses (Airbnb scraper -> server -> app "available now" popup) ──────────

class HouseListingIn(BaseModel):
    url: str
    title: Optional[str] = ""
    location: str
    check_in: Optional[str] = None
    check_out: Optional[str] = None
    price_raw: Optional[str] = None
    price_usd_per_night: Optional[float] = None
    images: List[str] = []
    lat: Optional[float] = None
    lng: Optional[float] = None
    # ── price breakdown (all optional so older scraper builds still ingest) ──
    listing_id: Optional[str] = None           # Airbnb room id, e.g. "884757326720093073"
    price_currency: Optional[str] = None       # "ZAR" | "USD"
    price_zar_per_night: Optional[float] = None
    fx_rate_zar_per_usd: Optional[float] = None
    nights: Optional[int] = None
    refresh_job_id: Optional[int] = None       # set by run_ondemand_job()
    neighbourhood: Optional[str] = None        # e.g. "Greendale"
    capacity: Optional[str] = None             # "6 guests · 3 bedrooms · 3 beds · 2.5 baths"
    rating: Optional[str] = None               # "4.67"
    reviews_count: Optional[str] = None        # "3"


class HouseIngestIn(BaseModel):
    """Body the scraper (airbnb_parallel_system.py) POSTs to /api/houses/ingest."""
    listings: List[HouseListingIn]


class HouseSendIn(BaseModel):
    """Body the app's house-icon popup POSTs to /api/houses/send after the
    guest/agent picks which of the (up to 6) auto-populated listings to share."""
    chat_id: str
    listing_ids: List[str]
    sender: str = "Front Desk"
    max_images_per_listing: int = 5
    # Parallel to listing_ids: the offer (exact dates/price) each pick was
    # shown with, so the caption quotes that window. Optional/legacy-safe.
    offer_ids: List[Optional[str]] = []


class HouseRefreshRequestIn(BaseModel):
    """Body the Android app POSTs to POST /api/houses/refresh the moment an
    exact-dates search against house_listings comes back empty
    (ChatRepository.requestHouseRefresh / HouseRefreshRequest DTO).
    check_in/check_out are plain 'YYYY-MM-DD' strings — kept as str rather
    than a Pydantic date type to match every other date field in this file
    (HouseListingIn, InvoiceCreateIn, ...), which are all str."""
    location: str
    # Optional now: a city-only search still queues a job, priced for the
    # server's DEFAULT_CHECKIN_DAYS_FROM_NOW / DEFAULT_STAY_NIGHTS window.
    check_in: Optional[str] = None
    check_out: Optional[str] = None


class HouseRefreshCompleteIn(BaseModel):
    """Body airbnb_parallel_system.py's ondemand_poll_loop() POSTs to
    POST /api/houses/refresh/{id}/complete once it has finished (or failed
    to find anything for) a job."""
    pushed: int = 0
    error_message: Optional[str] = None


def poll_options_with_counts(conn, poll_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT po.id, po.text,
               (SELECT COUNT(*) FROM poll_votes pv
                WHERE pv.poll_id = po.poll_id AND pv.option_id = po.id) AS votes
        FROM poll_options po
        WHERE po.poll_id = ?
        ORDER BY po.rowid ASC
        """,
        (poll_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def serialize_message(conn, row) -> dict:
    """Turn a `messages` row into the JSON shape clients expect (times at the
    display offset — see config.DISPLAY_TZ_OFFSET_MINUTES)."""
    from .config import DISPLAY_TZ_OFFSET_MINUTES
    from .wa_clean import to_offset_iso
    msg = dict(row)
    if msg.get("created_at"):
        msg["created_at"] = to_offset_iso(msg["created_at"], DISPLAY_TZ_OFFSET_MINUTES)
    if msg.get("kind") == "poll" and msg.get("poll_id"):
        msg["options"] = poll_options_with_counts(conn, msg["poll_id"])
    return msg
