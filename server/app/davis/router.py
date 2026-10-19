"""davis/router.py — Davis's API, live feed and dashboard.

GET  /davis                                  dashboard (password-gated like "/")
WS   /api/davis/ws                           live event feed
GET  /api/davis/status                       settings, metrics, providers, funnel
PUT  /api/davis/settings                     mode / provider / model / pause / chat lists
GET  /api/davis/sessions                     conversations Davis knows, newest first
GET  /api/davis/sessions/{chat_id}           one conversation: frame, messages, events, drafts
POST /api/davis/sessions/{chat_id}/pause     {paused: bool}
POST /api/davis/sessions/{chat_id}/handoff   {active: bool}   manual "Take over" (Davis muted) / give back
POST /api/davis/sessions/{chat_id}/reset     forget the frame (stage back to new)
POST /api/davis/sessions/{chat_id}/run       process now (shadow: draft only)
GET  /api/davis/events                       recent events
GET  /api/davis/drafts                       drafts waiting for approval
POST /api/davis/drafts/{id}/approve          {text?, force?} → sends it
POST /api/davis/drafts/{id}/discard
GET  /api/davis/catalogue                    houses per city with Davis's flags
PATCH /api/davis/catalogue                   {listing_url, hidden?, featured?, note?}
GET  /api/davis/requests                     live-home requests to the Airbnb scraper (newest first)
POST /api/davis/requests                     {city, check_in, check_out} → ask the scraper for those dates
POST /api/davis/preview                      {text, chat_id?} → what Davis would do (nothing sent)

GET  /api/davis/lock-state · POST /api/davis/unlock {password} · POST /api/davis/lock
Everything under /api/davis/* (and the feed) needs the Karlon password
(KARLON_PASSWORD; DAVIS_ADMIN_TOKEN also works): the unlock cookie, or the
X-Davis-Token header for scripts.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Optional

import asyncio
import hashlib
import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .. import auth
from ..config import STATIC_DIR
from ..database import get_db
from ..models import serialize_message
from ..routers import houses
from . import catalogue, config, gate, nlu, policy, skills, store
from .engine import _Cat, engine
from .local_llm import model as local_model

router = APIRouter(tags=["davis"])            # the page and the lock itself: open


# ─────────────────────────── the lock ────────────────────────────────────────
# Everything about guests (conversations, drafts, settings, the live feed) is
# behind the Karlon password: the KARLON_PASSWORD Space secret (DAVIS_ADMIN_TOKEN
# also works). The page shows a blurred lock screen until it is entered; the
# server checks every /api/davis/* call and the feed, so the blur is not the
# protection, the server is. Without a password set, the dashboard stays locked.

LOCK_COOKIE = "davis_unlock"
LOCK_MAX_AGE = 30 * 24 * 3600


def _secrets() -> list[str]:
    return [p for p in (auth.PASSWORD, config.ADMIN_TOKEN) if p]


def _lock_token() -> str:
    key = "|".join(_secrets()).encode()
    return hmac.new(key, b"davis-dashboard-v1", hashlib.sha256).hexdigest()


def _unlocked(cookie_header: str = "", token: Optional[str] = None) -> bool:
    if not _secrets():
        return False
    if token and any(hmac.compare_digest(token.encode(), p.encode()) for p in _secrets()):
        return True
    if hmac.compare_digest(auth.cookie_value(cookie_header, LOCK_COOKIE), _lock_token()):
        return True
    return auth.karlon_cookie_ok(cookie_header)


def require_unlocked(request: Request, x_davis_token: Optional[str] = Header(default=None)) -> None:
    if not _secrets():
        raise HTTPException(403, "lock_not_configured: set the KARLON_PASSWORD secret on the Space")
    if not _unlocked(request.headers.get("cookie", ""), x_davis_token):
        raise HTTPException(401, "locked: enter the Karlon password")


api = APIRouter(tags=["davis"], dependencies=[Depends(require_unlocked)])   # everything else: locked


class UnlockIn(BaseModel):
    password: str


@router.get("/api/davis/lock-state")
def lock_state(request: Request, x_davis_token: Optional[str] = Header(default=None)):
    return {"configured": bool(_secrets()),
            "unlocked": _unlocked(request.headers.get("cookie", ""), x_davis_token)}


@router.post("/api/davis/unlock")
async def unlock(body: UnlockIn):
    if not _secrets():
        raise HTTPException(403, "lock_not_configured: set the KARLON_PASSWORD secret on the Space")
    if not any(hmac.compare_digest(body.password.encode(), p.encode()) for p in _secrets()):
        await asyncio.sleep(1.5)                          # slows down guessing
        raise HTTPException(401, "wrong password")
    resp = JSONResponse({"ok": True})
    resp.headers.append("set-cookie", f"{LOCK_COOKIE}={_lock_token()}; Max-Age={LOCK_MAX_AGE}; Path=/; HttpOnly; "
                                      "Secure; SameSite=None; Partitioned")
    return resp


@router.post("/api/davis/lock")
def lock():
    resp = JSONResponse({"ok": True})
    resp.headers.append("set-cookie", f"{LOCK_COOKIE}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=None; "
                                      "Partitioned")
    return resp


class SettingsIn(BaseModel):
    mode: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    paused: Optional[bool] = None
    only_chats: Optional[list[str]] = None
    ignore_chats: Optional[list[str]] = None
    llm_for: Optional[str] = None


class FlagIn(BaseModel):
    paused: Optional[bool] = None
    active: Optional[bool] = None


class ApproveIn(BaseModel):
    text: Optional[str] = None
    force: bool = False


class CatalogueFlagIn(BaseModel):
    listing_url: str
    hidden: Optional[bool] = None
    featured: Optional[bool] = None
    note: Optional[str] = None


class SkillIn(BaseModel):
    question: str
    answer: str


class SkillPatch(BaseModel):
    enabled: Optional[bool] = None
    answer: Optional[str] = None


class PreviewIn(BaseModel):
    text: str
    chat_id: Optional[str] = None


# ─────────────────────────── page + feed ─────────────────────────────────────

@router.get("/davis", response_class=HTMLResponse)
def dashboard():
    return (STATIC_DIR / "davis.html").read_text(encoding="utf-8")


@router.websocket("/api/davis/ws")      # not /ws/davis: /ws/{chat_id} would catch it
async def feed(ws: WebSocket):
    if not _unlocked(ws.headers.get("cookie", ""), ws.query_params.get("token")):
        await ws.close(code=4401)                          # locked: no guest data over the feed
        return
    await ws.accept()
    engine.listeners.add(ws)
    try:
        await ws.send_json({"type": "hello", "data": _status()})
        while True:
            await ws.receive_text()          # keep-alive pings from the page
    except WebSocketDisconnect:
        pass
    finally:
        engine.listeners.discard(ws)


# ─────────────────────────── status + settings ───────────────────────────────

def _funnel(conn) -> dict:
    rows = conn.execute("SELECT stage, COUNT(*) AS n FROM davis_sessions GROUP BY stage").fetchall()
    out = {s: 0 for s in policy.STAGES}
    out.update({r["stage"]: r["n"] for r in rows})
    out["handoff"] = conn.execute("SELECT COUNT(*) FROM davis_sessions WHERE handoff=1").fetchone()[0]
    out["followups"] = conn.execute("SELECT COALESCE(SUM(followups), 0) FROM davis_sessions").fetchone()[0]
    return out


def _status() -> dict:
    conn = get_db()
    try:
        st = store.get_settings(conn)
        pending = conn.execute("SELECT COUNT(*) FROM davis_drafts WHERE status='pending'").fetchone()[0]
        funnel = _funnel(conn)
    finally:
        conn.close()
    return {
        "settings": st, "running": engine.running, "metrics": engine.metrics.snapshot(),
        "llm": engine.router.state(), "drafts_pending": pending, "funnel": funnel,
        "limits": {"max_words": config.MAX_WORDS, "max_replies_per_turn": config.MAX_REPLIES_PER_TURN,
                   "debounce_sec": config.DEBOUNCE_SEC, "fresh_minutes": config.FRESH_MINUTES,
                   "human_quiet_minutes": config.HUMAN_QUIET_MINUTES, "concurrency": config.CONCURRENCY,
                   "options_per_send": config.OPTIONS_PER_SEND, "images_per_option": config.IMAGES_PER_OPTION,
                   "early_present": config.EARLY_PRESENT, "ai_retries": config.AI_RETRIES,
                   "ai_retry_sec": config.AI_RETRY_SEC, "refresh_tries": config.REFRESH_TRIES,
                   "refresh_timeout_minutes": config.REFRESH_TIMEOUT_MINUTES,
                   "followup_hours": config.FOLLOWUP_HOURS, "followup_max": config.FOLLOWUP_MAX},
        "local_model": local_model.status(), "lock_configured": bool(_secrets()),
        "karl": {"understand": config.KARL_UNDERSTAND, "max_words": config.KARL_MAX_WORDS,
                 "transcript": config.KARL_TRANSCRIPT, "model": config.LOCAL_MODEL_FILE},
        "modes": config.MODES, "providers": config.PROVIDERS,
    }


@api.get("/api/davis/status")
def status():
    return _status()


@api.put("/api/davis/settings")
async def put_settings(body: SettingsIn):
    changes = body.model_dump(exclude_none=True)
    if "mode" in changes and changes["mode"] not in config.MODES:
        raise HTTPException(400, f"mode must be one of {config.MODES}")
    if "provider" in changes and changes["provider"] not in config.PROVIDERS:
        raise HTTPException(400, f"provider must be one of {config.PROVIDERS}")
    if "llm_for" in changes and changes["llm_for"] not in ("ai", "all", "open"):
        raise HTTPException(400, "llm_for must be ai, all or open")
    st = store.put_settings(changes)
    if changes.get("provider") == "local":
        engine.warm_local()
    await engine.emit(None, "settings", changes)
    return st


# ─────────────────────────── sessions ────────────────────────────────────────

@api.get("/api/davis/sessions")
def sessions(limit: int = 100):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT s.*, c.name AS chat_name, c.profile_pic_url, "
            " (SELECT text FROM messages m WHERE m.chat_id=s.chat_id ORDER BY rowid DESC LIMIT 1)"
            "   AS last_text, "
            " (SELECT direction FROM messages m WHERE m.chat_id=s.chat_id ORDER BY rowid DESC LIMIT 1) AS last_direction, "
            " (SELECT created_at FROM messages m WHERE m.chat_id=s.chat_id ORDER BY rowid DESC LIMIT 1) AS last_at, "
            " (SELECT COUNT(*) FROM davis_drafts d WHERE d.chat_id=s.chat_id AND d.status='pending') AS drafts "
            "FROM davis_sessions s LEFT JOIN chats c ON c.id=s.chat_id ORDER BY s.updated_at DESC LIMIT ?",
            (max(1, min(limit, 500)),)).fetchall()
        out = []
        for r in rows:
            d = store._session_row(r)
            d["chat_name"], d["last_text"] = r["chat_name"], (r["last_text"] or "")[:140]
            d["last_direction"], d["last_at"], d["drafts"] = r["last_direction"], r["last_at"], r["drafts"]
            d["profile_pic_url"] = r["profile_pic_url"]
            out.append(d)
        return out
    finally:
        conn.close()


@api.get("/api/davis/sessions/{chat_id}")
def session_detail(chat_id: str):
    conn = get_db()
    try:
        chat = conn.execute("SELECT id, name, profile_pic_url FROM chats WHERE id=?", (chat_id,)).fetchone()
        if not chat:
            raise HTTPException(404, "chat not found")
        rows = conn.execute("SELECT * FROM messages WHERE chat_id=? ORDER BY rowid DESC LIMIT 60",
                            (chat_id,)).fetchall()
        return {
            "chat": dict(chat), "session": store.get_session(conn, chat_id),
            "messages": [serialize_message(conn, r) for r in reversed(rows)],
            "offers": catalogue.offered(conn, chat_id),
            "events": store.events(conn, chat_id, 60),
            "drafts": [d for d in store.drafts(conn, None, 20) if d["chat_id"] == chat_id],
        }
    finally:
        conn.close()


def _need_session(conn, chat_id: str) -> None:
    if not conn.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone():
        raise HTTPException(404, "chat not found")
    store.get_session(conn, chat_id)


@api.post("/api/davis/sessions/{chat_id}/pause")
async def pause(chat_id: str, body: FlagIn):
    conn = get_db()
    try:
        _need_session(conn, chat_id)
        store.update_session(conn, chat_id, paused=bool(body.paused))
    finally:
        conn.close()
    await engine.emit(chat_id, "paused" if body.paused else "resumed")
    return {"ok": True, "paused": bool(body.paused)}


@api.post("/api/davis/sessions/{chat_id}/handoff")
async def handoff(chat_id: str, body: FlagIn):
    conn = get_db()
    try:
        _need_session(conn, chat_id)
        store.update_session(conn, chat_id, handoff=bool(body.active),
                             handoff_reason="set from dashboard" if body.active else None)
    finally:
        conn.close()
    await engine.emit(chat_id, "handoff" if body.active else "handoff_cleared")
    return {"ok": True, "handoff": bool(body.active)}


@api.post("/api/davis/sessions/{chat_id}/reset")
async def reset(chat_id: str):
    conn = get_db()
    try:
        _need_session(conn, chat_id)
        store.update_session(conn, chat_id, stage="new", slots={}, choice=None, handoff=False, handoff_reason=None,
                             refresh_job_id=None, refresh_tries=0, ai_retries=0, followups=0, invoice_key=None)
    finally:
        conn.close()
    await engine.emit(chat_id, "reset")
    return {"ok": True}


@api.post("/api/davis/sessions/{chat_id}/run")
async def run_now(chat_id: str, trigger: str = Query("message", pattern="^(message|refresh_done|refresh_timeout|followup)$"),
                  again: bool = False):
    """Process the chat now, without waiting for the debounce. In shadow mode
    (or mode off) this only drafts; in live mode it sends. again=true
    re-handles the guest's newest message even if Davis already did."""
    st = store.get_settings()
    if again:
        conn = get_db()
        try:
            _need_session(conn, chat_id)
            store.update_session(conn, chat_id, last_inbound_id=None)
        finally:
            conn.close()
    return await engine.process(chat_id, trigger=trigger, force=st["mode"] != "live")


# ─────────────────────────── events + drafts ─────────────────────────────────

@api.get("/api/davis/events")
def get_events(limit: int = 100, chat_id: Optional[str] = None):
    conn = get_db()
    try:
        return store.events(conn, chat_id, max(1, min(limit, 500)))
    finally:
        conn.close()


@api.get("/api/davis/drafts")
def get_drafts(status_: Optional[str] = Query("pending", alias="status")):
    conn = get_db()
    try:
        return store.drafts(conn, None if status_ in ("", "all") else status_)
    finally:
        conn.close()


@api.post("/api/davis/drafts/{draft_id}/approve")
async def approve(draft_id: int, body: ApproveIn):
    return await engine.approve(draft_id, body.text, body.force)


@api.post("/api/davis/drafts/{draft_id}/discard")
async def discard(draft_id: int):
    conn = get_db()
    try:
        if not store.decide_draft(conn, draft_id, "discarded"):
            raise HTTPException(409, "draft not pending")
    finally:
        conn.close()
    await engine.emit(None, "draft_discarded", {"draft_id": draft_id})
    return {"ok": True}


# ─────────────────────────── catalogue ───────────────────────────────────────

@api.get("/api/davis/catalogue")
def get_catalogue(city: str = "all", limit: int = 120):
    rows = houses.available_listings(location=city, limit=max(1, min(limit, houses.MAX_AVAILABLE_LIMIT)),
                                     sort="ref", check_in=None, check_out=None, chat_id=None)
    conn = get_db()
    try:
        flags = store.catalogue_flags(conn)
        offers = {}
        for r in conn.execute("SELECT listing_url, COUNT(*) AS n, MIN(check_in) AS next_in FROM listing_offers "
                              "WHERE check_out >= ? GROUP BY listing_url", (date.today().isoformat(),)):
            offers[r["listing_url"]] = {"offers": r["n"], "next_check_in": r["next_in"]}
        sends = {r["listing_url"]: r["n"] for r in conn.execute(
            "SELECT listing_url, COUNT(DISTINCT chat_id) AS n FROM house_sends GROUP BY listing_url")}
        cities = [r["location"] for r in conn.execute(
            "SELECT location, COUNT(*) AS n FROM house_listings GROUP BY location ORDER BY n DESC")]
    finally:
        conn.close()
    out = []
    for r in rows:
        f = flags.get(r["url"], {})
        out.append({
            "url": r["url"], "ref": r.get("ref_code"), "ref_no": r.get("ref_no"), "title": r.get("title"),
            "location": r.get("location"), "place": houses.place_of(r), "capacity": r.get("capacity"),
            "rating": r.get("rating"), "image": (r.get("images") or [None])[0],
            "usd_per_night": r.get("price_usd_per_night"), "updated_at": r.get("updated_at"),
            "hidden": bool(f.get("hidden")), "featured": bool(f.get("featured")), "note": f.get("note"),
            "sent_to_chats": sends.get(r["url"], 0), **offers.get(r["url"], {"offers": 0, "next_check_in": None}),
        })
    return {"cities": cities, "houses": out}


class LiveRequestIn(BaseModel):
    city: str
    check_in: str
    check_out: str


@api.get("/api/davis/requests")
def live_requests(limit: int = 30):
    """What Davis (or the dashboard) asked the Airbnb scraper for: the
    catalogue fills from these, for exactly the dates guests want."""
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM refresh_jobs ORDER BY created_at DESC LIMIT ?",
                            (max(1, min(limit, 200)),)).fetchall()
        waiting = {r["refresh_job_id"]: r["n"] for r in conn.execute(
            "SELECT refresh_job_id, COUNT(*) AS n FROM davis_sessions WHERE stage='searching' "
            "AND refresh_job_id IS NOT NULL GROUP BY refresh_job_id")}
    finally:
        conn.close()
    return {"requests": [{**dict(r), "pushed_count": r["pushed_count"] or 0,
                          "guests_waiting": waiting.get(r["id"], 0)} for r in rows]}


@api.post("/api/davis/requests")
async def new_live_request(body: LiveRequestIn):
    from ..models import HouseRefreshRequestIn
    from ..routers import house_refresh
    job = house_refresh.request_refresh(HouseRefreshRequestIn(location=body.city, check_in=body.check_in,
                                                              check_out=body.check_out))
    await engine.emit(None, "live_request", {k: job[k] for k in ("id", "location", "check_in", "check_out",
                                                                  "status")})
    return job


@api.patch("/api/davis/catalogue")
async def patch_catalogue(body: CatalogueFlagIn):
    conn = get_db()
    try:
        if not conn.execute("SELECT 1 FROM house_listings WHERE id=?", (body.listing_url,)).fetchone():
            raise HTTPException(404, "listing not found")
        row = store.set_catalogue_flag(conn, body.listing_url, hidden=body.hidden, featured=body.featured,
                                       note=body.note)
    finally:
        conn.close()
    catalogue.clear_cache()
    await engine.emit(None, "catalogue", {k: row[k] for k in ("listing_url", "hidden", "featured", "note")})
    return row


# ─────────────────────────── skills ──────────────────────────────────────────
# Answers learned from the team's replies in chats (and added by hand): Davis
# reuses them for similar questions instead of "I'll check" (skills.py).

@api.get("/api/davis/skills")
def get_skills():
    conn = get_db()
    try:
        return skills.listing(conn)
    finally:
        conn.close()


@api.post("/api/davis/skills")
async def add_skill(body: SkillIn):
    conn = get_db()
    try:
        row = skills.add(conn, body.question, body.answer)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()
    await engine.emit(None, "skills", {"added": row["question"][:80]})
    return row


@api.patch("/api/davis/skills/{skill_id}")
def patch_skill(skill_id: int, body: SkillPatch):
    conn = get_db()
    try:
        if not conn.execute("SELECT 1 FROM davis_skills WHERE id=?", (skill_id,)).fetchone():
            raise HTTPException(404, "skill not found")
        if body.enabled is not None:
            conn.execute("UPDATE davis_skills SET enabled=?, updated_at=? WHERE id=?",
                         (int(body.enabled), store.now_iso(), skill_id))
        if body.answer is not None and body.answer.strip():
            conn.execute("UPDATE davis_skills SET answer=?, updated_at=? WHERE id=?",
                         (nlu.clip_words(body.answer.strip(), config.MAX_WORDS), store.now_iso(), skill_id))
        conn.commit()
        return dict(conn.execute("SELECT * FROM davis_skills WHERE id=?", (skill_id,)).fetchone())
    finally:
        conn.close()


@api.delete("/api/davis/skills/{skill_id}")
def delete_skill(skill_id: int):
    conn = get_db()
    try:
        conn.execute("DELETE FROM davis_skills WHERE id=?", (skill_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@api.post("/api/davis/skills/learn")
def learn_skills():
    conn = get_db()
    try:
        return {"learned": skills.learn(conn), "total": len(skills.listing(conn))}
    finally:
        conn.close()


# ─────────────────────────── preview ─────────────────────────────────────────

@api.post("/api/davis/preview")
async def preview(body: PreviewIn):
    """What Davis would make of a message: features, the plan and the reply
    (template and, if a model is configured, the model's version). Nothing
    is stored or sent."""
    t0 = time.monotonic()
    conn = get_db()
    try:
        today = engine._today()
        places = catalogue.places(conn)
        feats = policy.turn_features([body.text], today, places)
        session = {"stage": "new", "slots": {}, "choice": None}
        chat_name, offers = "Guest", []
        if body.chat_id and conn.execute("SELECT 1 FROM chats WHERE id=?", (body.chat_id,)).fetchone():
            session = dict(store.get_session(conn, body.chat_id))
            chat_name = conn.execute("SELECT name FROM chats WHERE id=?", (body.chat_id,)).fetchone()["name"]
            offers = catalogue.offered(conn, body.chat_id)
        ctx = {"chat_id": body.chat_id or "preview", "chat_name": chat_name, "session": session,
               "offers": offers, "messages": [], "settings": store.get_settings(conn)}
        plan_ = policy.plan(ctx, feats, _Cat(conn, body.chat_id or "preview"))
        template = next((a["text"] for a in plan_["actions"] if a["type"] == "reply"), "")
        settings = ctx["settings"]
        ctx["messages"] = [{"direction": "in", "sender": chat_name, "kind": "text", "text": body.text,
                            "created_at": store.now_iso()}]
        if settings.get("provider", "rules") != "rules":
            plan_ = await engine._phrase(ctx, feats, plan_, _Cat(conn, body.chat_id or "preview"), settings)
    finally:
        conn.close()
    from .engine import _brief
    return {"features": {k: v for k, v in feats.items() if v and k not in ("texts", "empty")},
            "words_limit": config.MAX_WORDS, "stage": plan_["stage"], "intent": plan_.get("intent"),
            "slots": plan_["slots"], "template": template, "actions": [_brief(a) for a in plan_["actions"]],
            "llm": plan_.get("llm"), "ms": int((time.monotonic() - t0) * 1000)}
