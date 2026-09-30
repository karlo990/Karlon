"""routers/team_location.py — live team location tracking.

Each staff device POSTs its current GPS fix here periodically (see the
Android LocationTrackingService). We keep only the latest fix per
member_id (upsert), and push every update out over the existing
WebSocket channel so any open web dashboard updates live without
polling.

The web UI (static/index.html) connects to /ws/team_locations and also
does an initial GET /api/team/locations to seed the map on load.
"""

from datetime import datetime, timezone

from fastapi import APIRouter

from ..database import get_db
from ..models import LocationIn
from ..ws_manager import manager

router = APIRouter(prefix="/api/team", tags=["team_location"])

# Channel key used with the existing chat-agnostic ConnectionManager —
# it's keyed by an arbitrary string, not strictly a chat_id, so we can
# reuse it here for a non-chat broadcast channel.
LOCATIONS_CHANNEL = "team_locations"


@router.post("/location")
async def update_location(body: LocationIn):
    """Upsert a member's latest known position."""
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO team_locations
            (member_id, member_name, latitude, longitude, accuracy_m, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(member_id) DO UPDATE SET
            member_name = excluded.member_name,
            latitude    = excluded.latitude,
            longitude   = excluded.longitude,
            accuracy_m  = excluded.accuracy_m,
            updated_at  = excluded.updated_at
        """,
        (body.member_id, body.member_name, body.latitude, body.longitude,
         body.accuracy_m, now),
    )
    conn.commit()
    conn.close()

    await manager.broadcast(LOCATIONS_CHANNEL, {
        "type": "location_update",
        "data": {
            "member_id": body.member_id,
            "member_name": body.member_name,
            "latitude": body.latitude,
            "longitude": body.longitude,
            "accuracy_m": body.accuracy_m,
            "updated_at": now,
        },
    })
    return {"ok": True}


@router.get("/locations")
def get_locations():
    """Latest known position for every member who has ever reported one."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM team_locations ORDER BY updated_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
