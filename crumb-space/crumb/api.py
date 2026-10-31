"""crumb/api.py — the public API: exactly what Sunday Crumb's chat widget
calls (GET /api/assistant, POST /api/session, POST /api/chat), the bakery as
data, and every planned endpoint answering 501 with what it will do."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import config, endpoints, guard, knowledge, llm, store

router = APIRouter()


def _err(status: int, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


# ── the widget ──────────────────────────────────────────────────────────────

@router.get("/api/assistant")
def assistant_settings():
    s = store.settings()
    out = {k: s[k] for k in ("name", "business", "greeting", "notice", "suggestions", "theme", "ready")}
    out["handoff"] = None
    if config.TURNSTILE_ON:
        out["turnstileSiteKey"] = config.TURNSTILE_SITE_KEY
    return out


class SessionIn(BaseModel):
    token: str = ""


@router.post("/api/session")
async def new_session(body: SessionIn, request: Request):
    if not config.TURNSTILE_ON:
        return {"ok": True, "check": "off"}
    ip = guard.client_ip(request)
    ok, reason = await guard.verify_turnstile(body.token, ip)
    if not ok:
        store.event("session_failed", reason=reason)
        return _err(403, "We couldn’t check that you’re a person. Please try again.", code="turnstile")
    sid = uuid.uuid4().hex
    resp = JSONResponse({"ok": True, "minutes": config.SESSION_MINUTES})
    guard.set_partitioned(resp, config.SESSION_COOKIE, guard.make_session(sid), config.SESSION_MINUTES * 60)
    store.event("session_ok")
    return resp


class ChatMessage(BaseModel):
    role: str
    content: str = Field(default="", max_length=4000)


class ChatIn(BaseModel):
    messages: list[ChatMessage] = Field(default_factory=list, max_length=50)


@router.post("/api/chat")
async def chat(body: ChatIn, request: Request):
    s = store.settings()
    if not s.get("ready"):
        return _err(503, "The chat isn’t switched on yet. Please try again later.")
    ip = guard.client_ip(request)
    sid = guard.read_session(request.cookies.get(config.SESSION_COOKIE))
    if config.TURNSTILE_ON and not sid:
        return _err(401, "Your chat session expired. Please try again.", code="session")
    conv = sid or guard.anon_id(ip)
    if not guard.per_ip.allow(ip) or not guard.per_session.allow(conv):
        store.event("rate_limited", conversation=conv[:12])
        return _err(429, "That’s a lot of questions at once! Please wait a few minutes and try again.")
    history = llm.clean_history([m.model_dump() for m in body.messages])
    if not history or history[-1]["role"] != "user":
        return _err(400, "Please type a question first.")
    question = history[-1]["content"]
    if llm.available() and not store.take_daily():
        result = llm.Reply(knowledge.offline_answer(question), "offline", error="daily_limit")
    else:
        result = await llm.reply(history, s.get("notes") or "")
    store.log_turn(conv, question, result.text, result.source, result.latency_ms,
                   result.input_tokens, result.output_tokens)
    store.event("error" if result.error and result.error not in ("no_api_key",) else "chat",
                conversation=conv[:12], source=result.source, ms=result.latency_ms, error=result.error)
    return {"text": result.text}


# ── the bakery, as data ─────────────────────────────────────────────────────

@router.get("/api/menu")
def menu():
    return {"currency": knowledge.CURRENCY, "illustrative": True, "products": knowledge.PRODUCTS}


@router.get("/api/story")
def story():
    return {"business": knowledge.BUSINESS, "studio": knowledge.STUDIO, "story": knowledge.STORY,
            "taglines": knowledge.TAGLINES, "rules": knowledge.RULES}


class BoxIn(BaseModel):
    items: dict[str, int] = Field(default_factory=dict)


@router.post("/api/box/quote")
def box_quote(body: BoxIn):
    quote = knowledge.box_quote(body.items)
    return JSONResponse(quote, status_code=400 if quote["errors"] else 200)


class FeedbackIn(BaseModel):
    rating: str
    comment: str = Field(default="", max_length=1000)


@router.post("/api/feedback")
def feedback(body: FeedbackIn, request: Request):
    if body.rating not in ("up", "down"):
        return _err(400, "rating must be 'up' or 'down'")
    sid = guard.read_session(request.cookies.get(config.SESSION_COOKIE)) or guard.anon_id(guard.client_ip(request))
    return {"ok": True, "id": store.add_feedback(sid, body.rating, body.comment.strip())}


# ── operations ──────────────────────────────────────────────────────────────

@router.get("/api/health")
def health():
    return {"ok": True, "service": "sunday-crumb", "version": config.VERSION, "model": config.MODEL,
            "model_configured": llm.available(), "person_check": config.TURNSTILE_ON,
            "dashboard_locked": bool(config.ADMIN_PASSWORD)}


@router.get("/api/endpoints")
def endpoint_catalogue():
    return endpoints.catalogue()


# ── planned endpoints: the contract exists before the feature ──────────────

def _planned_handler(spec: dict):
    async def handler(request: Request, order_id: Optional[str] = None, member_id: Optional[str] = None):
        return JSONResponse({"error": "Not built yet.", "planned": True, "method": spec["method"],
                             "path": spec["path"], "summary": spec["summary"], "body": spec.get("body")},
                            status_code=501)
    return handler


for _spec in endpoints.planned():
    router.add_api_route(_spec["path"], _planned_handler(_spec), methods=[_spec["method"]],
                         summary=f"(planned) {_spec['summary']}", tags=["planned"])
