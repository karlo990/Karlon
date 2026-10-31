"""crumb/guard.py — who may chat: the Turnstile "are you a person" check,
the signed session cookie it is swapped for, and the rate limits."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections import defaultdict, deque
from typing import Optional

import httpx
from fastapi import Request

from . import config


# ── Turnstile ───────────────────────────────────────────────────────────────

async def verify_turnstile(token: str, ip: Optional[str]) -> tuple[bool, str]:
    """(ok, reason). Cloudflare's siteverify; each token works once."""
    if not token or len(token) > 2048:
        return False, "missing_token"
    data = {"secret": config.TURNSTILE_SECRET_KEY, "response": token}
    if ip:
        data["remoteip"] = ip
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(config.TURNSTILE_VERIFY_URL, data=data)
        body = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return False, f"verify_unreachable:{type(e).__name__}"
    if body.get("success"):
        return True, "ok"
    return False, ",".join(body.get("error-codes") or ["failed"])


# ── session cookie ──────────────────────────────────────────────────────────

def _sign(payload: bytes) -> str:
    return hmac.new(config.SECRET.encode(), payload, hashlib.sha256).hexdigest()


def make_session(sid: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"sid": sid, "exp": int(time.time()) + config.SESSION_MINUTES * 60})
                                       .encode()).decode()
    return f"{payload}.{_sign(payload.encode())}"


def read_session(value: Optional[str]) -> Optional[str]:
    """The session id, or None if the cookie is missing, forged or expired."""
    if not value or "." not in value:
        return None
    payload, sig = value.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(payload.encode())):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
    except ValueError:
        return None
    return data.get("sid") if data.get("exp", 0) > time.time() else None


def cookie_kwargs(max_age: int) -> dict:
    # SameSite=None + Partitioned so it also works inside the Hugging Face
    # page's iframe; Secure is required for both.
    return {"max_age": max_age, "httponly": True, "secure": True, "samesite": "none", "path": "/"}


def set_partitioned(response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(name, value, **cookie_kwargs(max_age))
    # Starlette has no "Partitioned" flag yet: append it to the header just set.
    raw = response.raw_headers
    for i in range(len(raw) - 1, -1, -1):
        k, v = raw[i]
        if k == b"set-cookie" and v.startswith(name.encode() + b"="):
            raw[i] = (k, v + b"; Partitioned")
            break


# ── who is this ─────────────────────────────────────────────────────────────

def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() or (request.client.host if request.client else "")) or "unknown"


def anon_id(ip: str) -> str:
    """A stable, non-reversible id for a visitor without a session (Turnstile off)."""
    return "ip-" + hashlib.sha256((config.SECRET + ip).encode()).hexdigest()[:16]


# ── rate limits ─────────────────────────────────────────────────────────────

class RateLimiter:
    """Sliding window: at most `limit` hits per `window` seconds per key."""

    def __init__(self, limit: int, window: int) -> None:
        self.limit, self.window = limit, window
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, now: Optional[float] = None) -> bool:
        now = now or time.time()
        q = self.hits[key]
        while q and q[0] <= now - self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        if len(self.hits) > 50_000:              # forget idle visitors
            for k in [k for k, v in self.hits.items() if not v][:10_000]:
                self.hits.pop(k, None)
        return True


per_session = RateLimiter(config.RATE_PER_SESSION, config.RATE_WINDOW_SEC)
per_ip = RateLimiter(config.RATE_PER_IP, config.RATE_WINDOW_SEC)
