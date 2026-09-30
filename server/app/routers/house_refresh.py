"""routers/house_refresh.py — on-demand Airbnb re-price jobs for exact dates.

NOTE: the previous commit of this file was accidentally a copy of
database.py — it defined no `router`, so `from .routers import house_refresh`
in main.py raised AttributeError on `house_refresh.router` and the Space
never came up (that is what invoice_worker / wa_bridge were timing out on).
This is the real implementation.

Contract (shared with airbnb_parallel_system.py's ondemand_poll_loop and the
Android app's ChatDetailViewModel.loadHouses):

    POST /api/houses/refresh                 {location, check_in?, check_out?}
         -> {id, status, location, check_in, check_out, pushed_count}
         Idempotent per (location, check_in, check_out): if a job for that
         window is already pending/in_progress, that job is returned instead
         of queuing a duplicate. Missing dates default to the server's
         DEFAULT_CHECKIN_DAYS_FROM_NOW / DEFAULT_STAY_NIGHTS window.

    GET  /api/houses/refresh/pending         (scraper)
         -> [{id, location, check_in, check_out}, ...]
         Atomically flips returned jobs pending -> in_progress so two scraper
         instances never work the same job. Jobs stuck in_progress for longer
         than REFRESH_JOB_STALE_MINUTES are handed out again.

    POST /api/houses/refresh/{id}/complete   {pushed, error_message?}  (scraper)

    GET  /api/houses/refresh/{id}            (app polls this for live progress)
         -> {id, status, pushed_count, ...}

    GET  /api/houses/refresh/status?location=&check_in=&check_out=
         -> newest job for that window (or 404) — lets the app find the job
            it should track without holding on to an id.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException

from ..config import (
    DEFAULT_CHECKIN_DAYS_FROM_NOW,
    DEFAULT_STAY_NIGHTS,
    REFRESH_JOB_STALE_MINUTES,
)
from ..database import get_db
from ..models import HouseRefreshCompleteIn, HouseRefreshRequestIn

router = APIRouter(prefix="/api/houses/refresh", tags=["house-refresh"])


def _default_window() -> tuple[str, str]:
    ci = date.today() + timedelta(days=DEFAULT_CHECKIN_DAYS_FROM_NOW)
    co = ci + timedelta(days=DEFAULT_STAY_NIGHTS)
    return ci.isoformat(), co.isoformat()


def _validate_window(check_in: str, check_out: str) -> None:
    try:
        ci = date.fromisoformat(check_in)
        co = date.fromisoformat(check_out)
    except ValueError:
        raise HTTPException(400, "check_in/check_out must be YYYY-MM-DD")
    if ci < date.today():
        raise HTTPException(400, "check_in must be today or later")
    if co <= ci:
        raise HTTPException(400, "check_out must be after check_in")


def _job(row) -> dict:
    d = dict(row)
    d["pushed_count"] = d.get("pushed_count") or 0
    return d


@router.post("")
def request_refresh(body: HouseRefreshRequestIn):
    loc = body.location.strip().lower()
    if not loc:
        raise HTTPException(400, "location is required")
    check_in, check_out = body.check_in, body.check_out
    if not (check_in and check_out):
        check_in, check_out = _default_window()
    _validate_window(check_in, check_out)

    conn = get_db()
    existing = conn.execute(
        "SELECT * FROM refresh_jobs WHERE location=? AND check_in=? AND check_out=? "
        "AND status IN ('pending','in_progress') ORDER BY created_at DESC LIMIT 1",
        (loc, check_in, check_out),
    ).fetchone()
    if existing:
        conn.close()
        return _job(existing)

    cur = conn.execute(
        "INSERT INTO refresh_jobs (location, check_in, check_out, status, pushed_count, created_at) "
        "VALUES (?, ?, ?, 'pending', 0, ?)",
        (loc, check_in, check_out, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM refresh_jobs WHERE id=?", (cur.lastrowid,)).fetchone()
    conn.close()
    return _job(row)


@router.get("/pending")
def pending_jobs():
    """Polled by the scraper. Hands out pending jobs (oldest first) and marks
    them in_progress in the same transaction. Also re-queues jobs that have
    been in_progress suspiciously long (scraper died mid-job)."""
    conn = get_db()
    now = datetime.now(timezone.utc)
    stale_before = (now - timedelta(minutes=REFRESH_JOB_STALE_MINUTES)).isoformat()
    conn.execute(
        "UPDATE refresh_jobs SET status='pending' "
        "WHERE status='in_progress' AND COALESCE(started_at, created_at) < ?",
        (stale_before,),
    )

    rows = conn.execute(
        "SELECT * FROM refresh_jobs WHERE status='pending' ORDER BY created_at ASC LIMIT 5"
    ).fetchall()
    ids = [r["id"] for r in rows]
    if ids:
        marks = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE refresh_jobs SET status='in_progress', started_at=? WHERE id IN ({marks})",
            (now.isoformat(), *ids),
        )
        conn.commit()
    conn.close()
    return [
        {"id": r["id"], "location": r["location"], "check_in": r["check_in"], "check_out": r["check_out"]}
        for r in rows
    ]


@router.get("/status")
def refresh_status(location: str, check_in: Optional[str] = None, check_out: Optional[str] = None):
    """Newest job for a (location, window) — what the app tracks while polling."""
    loc = location.strip().lower()
    if not (check_in and check_out):
        check_in, check_out = _default_window()
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM refresh_jobs WHERE location=? AND check_in=? AND check_out=? "
        "ORDER BY created_at DESC LIMIT 1",
        (loc, check_in, check_out),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "no refresh job for that window")
    return _job(row)


@router.get("/{job_id}")
def get_job(job_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM refresh_jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "job not found")
    return _job(row)


@router.post("/{job_id}/complete")
def complete_job(job_id: int, body: HouseRefreshCompleteIn):
    conn = get_db()
    row = conn.execute("SELECT * FROM refresh_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "job not found")
    # pushed_count was incremented live by /ingest for each listing that
    # carried refresh_job_id; take the larger of that and what the scraper
    # reports so an older scraper build (no refresh_job_id) still records it.
    live = row["pushed_count"] or 0
    conn.execute(
        "UPDATE refresh_jobs SET status=?, pushed_count=?, completed_at=?, error_message=? WHERE id=?",
        (
            "error" if body.error_message else "done",
            max(live, body.pushed or 0),
            datetime.now(timezone.utc).isoformat(),
            body.error_message,
            job_id,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM refresh_jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    return _job(row)
