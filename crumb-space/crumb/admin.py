"""crumb/admin.py — Crumb's kitchen: the dashboard and its API, behind
CRUMB_ADMIN_PASSWORD (an HttpOnly cookie after unlocking, or the
X-Crumb-Token header for scripts). Without a password set it stays locked."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import config, endpoints, guard, knowledge, llm, store

public = APIRouter()
router = APIRouter()
_started = time.time()


def _token() -> str:
    return hashlib.sha256(("crumb-admin|" + config.ADMIN_PASSWORD + "|" + config.SECRET).encode()).hexdigest()


def _ok(cookie: Optional[str], header: Optional[str]) -> bool:
    if not config.ADMIN_PASSWORD:
        return False
    if header and hmac.compare_digest(header, config.ADMIN_PASSWORD):
        return True
    return bool(cookie) and hmac.compare_digest(cookie, _token())


async def require_admin(request: Request, x_crumb_token: Optional[str] = Header(default=None)) -> None:
    if not _ok(request.cookies.get(config.ADMIN_COOKIE), x_crumb_token):
        raise HTTPException(403, "locked" if config.ADMIN_PASSWORD else "set CRUMB_ADMIN_PASSWORD to unlock")


# ── public: the page and the lock ───────────────────────────────────────────

@public.get("/admin", include_in_schema=False)
def dashboard_page():
    return FileResponse(config.STATIC / "admin" / "index.html", headers={"Cache-Control": "no-store"})


@public.get("/api/admin/lock-state")
def lock_state(request: Request, x_crumb_token: Optional[str] = Header(default=None)):
    return {"configured": bool(config.ADMIN_PASSWORD),
            "unlocked": _ok(request.cookies.get(config.ADMIN_COOKIE), x_crumb_token)}


class UnlockIn(BaseModel):
    password: str


@public.post("/api/admin/unlock")
async def unlock(body: UnlockIn):
    if not config.ADMIN_PASSWORD or not hmac.compare_digest(body.password, config.ADMIN_PASSWORD):
        await asyncio.sleep(1.5)                      # slow down guessing
        raise HTTPException(403, "wrong password" if config.ADMIN_PASSWORD else "no password configured")
    resp = JSONResponse({"ok": True})
    guard.set_partitioned(resp, config.ADMIN_COOKIE, _token(), 12 * 3600)
    return resp


@public.post("/api/admin/lock")
def lock():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(config.ADMIN_COOKIE, path="/")
    return resp


# ── behind the lock ─────────────────────────────────────────────────────────

@router.get("/api/admin/status", dependencies=[Depends(require_admin)])
def status():
    return {"stats": store.stats(), "settings": store.settings(), "uptime_s": int(time.time() - _started),
            "config": {"model": config.MODEL, "effort": config.EFFORT, "model_configured": llm.available(),
                       "person_check": config.TURNSTILE_ON, "turnstile_site_key": config.TURNSTILE_SITE_KEY,
                       "daily_limit": config.DAILY_LIMIT, "rate_per_session": config.RATE_PER_SESSION,
                       "rate_per_ip": config.RATE_PER_IP, "rate_window_s": config.RATE_WINDOW_SEC,
                       "version": config.VERSION},
            "endpoints": {k: v for k, v in endpoints.catalogue().items() if k != "endpoints"}}


@router.get("/api/admin/conversations", dependencies=[Depends(require_admin)])
def conversations(limit: int = 50):
    return store.conversations(max(1, min(limit, 200)))


@router.get("/api/admin/conversations/{conversation_id}", dependencies=[Depends(require_admin)])
def conversation(conversation_id: str):
    c = store.conversation(conversation_id)
    if not c:
        raise HTTPException(404, "not found")
    return c


@router.get("/api/admin/events", dependencies=[Depends(require_admin)])
def events(limit: int = 60):
    return store.events(max(1, min(limit, 500)))


@router.get("/api/admin/feedback", dependencies=[Depends(require_admin)])
def feedback():
    return store.feedback()


class SettingsIn(BaseModel):
    greeting: Optional[str] = Field(default=None, max_length=300)
    notice: Optional[str] = Field(default=None, max_length=300)
    suggestions: Optional[list[str]] = Field(default=None, max_length=6)
    ready: Optional[bool] = None
    notes: Optional[str] = Field(default=None, max_length=4000)


@router.put("/api/admin/settings", dependencies=[Depends(require_admin)])
def put_settings(body: SettingsIn):
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if "suggestions" in changes:
        changes["suggestions"] = [s.strip()[:80] for s in changes["suggestions"] if s.strip()]
    out = store.put_settings(changes)
    store.event("settings", changed=sorted(changes))
    return out


class PreviewIn(BaseModel):
    question: str = Field(max_length=1000)


@router.post("/api/admin/preview", dependencies=[Depends(require_admin)])
async def preview(body: PreviewIn):
    """Try Crumb from the dashboard: not logged, not counted."""
    r = await llm.reply([{"role": "user", "content": body.question.strip()}], store.settings().get("notes") or "")
    return {"text": r.text, "source": r.source, "latency_ms": r.latency_ms, "error": r.error}


@router.get("/api/admin/endpoints", dependencies=[Depends(require_admin)])
def endpoint_catalogue():
    return endpoints.catalogue()


@router.get("/api/admin/knowledge", dependencies=[Depends(require_admin)])
def knowledge_view():
    return {"products": knowledge.PRODUCTS, "story": knowledge.STORY, "rules": knowledge.RULES,
            "system_prompt": knowledge.system_prompt(store.settings().get("notes") or "")}
