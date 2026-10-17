"""davis/router.py — Davis's API, live feed and dashboard.

GET  /davis                                  dashboard (password-gated like "/")
WS   /api/davis/ws                           live event feed
GET  /api/davis/status                       settings, metrics, providers, funnel
PUT  /api/davis/settings                     mode / provider / model / pause / chat lists
GET  /api/davis/sessions                     conversations Davis knows, newest first
GET  /api/davis/sessions/{chat_id}           one conversation: frame, messages, events, drafts
POST /api/davis/sessions/{chat_id}/pause     {paused: bool}
POST /api/davis/sessions/{chat_id}/handoff   {active: bool}   (false = give the chat back to Davis)
POST /api/davis/sessions/{chat_id}/reset     forget the frame (stage back to new)
POST /api/davis/sessions/{chat_id}/run       process now (shadow: draft only)
GET  /api/davis/events                       recent events
GET  /api/davis/drafts                       drafts waiting for approval
POST /api/davis/drafts/{id}/approve          {text?, force?} → sends it
POST /api/davis/drafts/{id}/discard
GET  /api/davis/catalogue                    houses per city with Davis's flags
PATCH /api/davis/catalogue                   {listing_url, hidden?, featured?, note?}
POST /api/davis/preview                      {text, chat_id?} → what Davis would do (nothing sent)

Writes need the DAVIS_ADMIN_TOKEN (X-Davis-Token header) when one is set.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..config import STATIC_DIR
from ..database import get_db
from ..models import serialize_message
from ..routers import houses
from . import catalogue, config, gate, nlu, policy, store
from .engine import _Cat, engine

router = APIRouter(tags=["davis"])


def admin(x_davis_token: Optional[str] = Header(default=None)) -> None:
    if config.ADMIN_TOKEN and x_davis_token != config.ADMIN_TOKEN:
        raise HTTPException(401, "Davis admin token required (X-Davis-Token)")


class SettingsIn(BaseModel):
    mode: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    effort: Optional[str] = None
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


class PreviewIn(BaseModel):
    text: str
    chat_id: Optional[str] = None


# ─────────────────────────── page + feed ─────────────────────────────────────

@router.get("/davis", response_class=HTMLResponse)
def dashboard():
    return (STATIC_DIR / "davis.html").read_text(encoding="utf-8")


@router.websocket("/api/davis/ws")      # not /ws/davis: /ws/{chat_id} would catch it
async def feed(ws: WebSocket):
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
                   "options_per_send": config.OPTIONS_PER_SEND, "images_per_option": config.IMAGES_PER_OPTION},
        "anthropic_key": bool(config.ANTHROPIC_API_KEY), "admin_token_required": bool(config.ADMIN_TOKEN),
        "modes": config.MODES, "providers": config.PROVIDERS,
    }


@router.get("/api/davis/status")
def status():
    return _status()


@router.put("/api/davis/settings", dependencies=[Depends(admin)])
async def put_settings(body: SettingsIn):
    changes = body.model_dump(exclude_none=True)
    if "mode" in changes and changes["mode"] not in config.MODES:
        raise HTTPException(400, f"mode must be one of {config.MODES}")
    if "provider" in changes and changes["provider"] not in config.PROVIDERS:
        raise HTTPException(400, f"provider must be one of {config.PROVIDERS}")
    if changes.get("provider") == "anthropic" and not config.ANTHROPIC_API_KEY:
        raise HTTPException(400, "set the ANTHROPIC_API_KEY secret on the Space first")
    if "llm_for" in changes and changes["llm_for"] not in ("all", "open"):
        raise HTTPException(400, "llm_for must be all or open")
    if "effort" in changes and changes["effort"] not in ("low", "medium", "high"):
        raise HTTPException(400, "effort must be low, medium or high")
    st = store.put_settings(changes)
    await engine.emit(None, "settings", changes)
    return st


# ─────────────────────────── sessions ────────────────────────────────────────

@router.get("/api/davis/sessions")
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


@router.get("/api/davis/sessions/{chat_id}")
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


@router.post("/api/davis/sessions/{chat_id}/pause", dependencies=[Depends(admin)])
async def pause(chat_id: str, body: FlagIn):
    conn = get_db()
    try:
        _need_session(conn, chat_id)
        store.update_session(conn, chat_id, paused=bool(body.paused))
    finally:
        conn.close()
    await engine.emit(chat_id, "paused" if body.paused else "resumed")
    return {"ok": True, "paused": bool(body.paused)}


@router.post("/api/davis/sessions/{chat_id}/handoff", dependencies=[Depends(admin)])
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


@router.post("/api/davis/sessions/{chat_id}/reset", dependencies=[Depends(admin)])
async def reset(chat_id: str):
    conn = get_db()
    try:
        _need_session(conn, chat_id)
        store.update_session(conn, chat_id, stage="new", slots={}, choice=None, handoff=False, handoff_reason=None,
                             refresh_job_id=None, invoice_key=None)
    finally:
        conn.close()
    await engine.emit(chat_id, "reset")
    return {"ok": True}


@router.post("/api/davis/sessions/{chat_id}/run", dependencies=[Depends(admin)])
async def run_now(chat_id: str, trigger: str = Query("message", pattern="^(message|refresh_done)$"),
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

@router.get("/api/davis/events")
def get_events(limit: int = 100, chat_id: Optional[str] = None):
    conn = get_db()
    try:
        return store.events(conn, chat_id, max(1, min(limit, 500)))
    finally:
        conn.close()


@router.get("/api/davis/drafts")
def get_drafts(status_: Optional[str] = Query("pending", alias="status")):
    conn = get_db()
    try:
        return store.drafts(conn, None if status_ in ("", "all") else status_)
    finally:
        conn.close()


@router.post("/api/davis/drafts/{draft_id}/approve", dependencies=[Depends(admin)])
async def approve(draft_id: int, body: ApproveIn):
    return await engine.approve(draft_id, body.text, body.force)


@router.post("/api/davis/drafts/{draft_id}/discard", dependencies=[Depends(admin)])
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

@router.get("/api/davis/catalogue")
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


@router.patch("/api/davis/catalogue", dependencies=[Depends(admin)])
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


# ─────────────────────────── preview ─────────────────────────────────────────

@router.post("/api/davis/preview")
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
