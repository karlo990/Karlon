"""auth.py — password on the HTML pages ONLY (web dashboard + /chat views).

Set it as a SECRET on the Hugging Face Space:
    Settings -> Variables and secrets -> New secret
    Name:  KARLON_PASSWORD      Value: <your password>
(Restart the Space after adding/changing it.) Unset = no password at all.

What is gated:   "/"  (web dashboard)  and  "/chat/..."  (chat view pages)
What stays OPEN: every /api/* and /ws/* endpoint, /static/* — so wa_bridge,
                 the workers, the scraper and the Android app need NO changes.

Browser flow: first visit shows a small login page -> correct password sets
a signed cookie (30 days) -> page loads normally. /logout clears it.
The cookie is SameSite=None; Secure; Partitioned so it also works inside the
huggingface.co/spaces iframe (a third-party context).
"""

from __future__ import annotations

import hashlib
import hmac
import html
import os
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Form
from starlette.responses import HTMLResponse, RedirectResponse

PASSWORD = os.environ.get("KARLON_PASSWORD", "").strip()
COOKIE = "karlon_web"
MAX_AGE = 30 * 24 * 3600


def _token() -> str:
    return hmac.new(PASSWORD.encode(), b"karlon-web-v1", hashlib.sha256).hexdigest()


def _gated(path: str) -> bool:
    return path == "/" or path.startswith("/chat/")


def _cookie_ok(scope) -> bool:
    for name, value in scope.get("headers") or []:
        if name.lower() == b"cookie":
            for part in value.decode("latin-1").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE and hmac.compare_digest(v, _token()):
                    return True
    return False


def _login_page(next_path: str, wrong: bool = False) -> HTMLResponse:
    msg = "Wrong password — try again." if wrong else "Enter the Karlon password."
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Karlon — sign in</title>
<style>body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f1417;font-family:system-ui,Segoe UI,Roboto,sans-serif;color:#eee}}
form{{background:#1c2226;padding:26px;border-radius:14px;width:min(320px,90vw);box-shadow:0 10px 40px rgba(0,0,0,.5)}}
h1{{font-size:20px;margin:0 0 6px}}p{{font-size:13px;opacity:.75;margin:0 0 14px}}
input{{width:100%;box-sizing:border-box;padding:11px;border-radius:8px;border:1px solid #444;background:#111;color:#eee;font-size:15px}}
button{{margin-top:14px;width:100%;padding:11px;border:0;border-radius:8px;background:#c7a24a;color:#111;font-weight:600;font-size:15px;cursor:pointer}}</style>
</head><body><form method="post" action="/login">
<h1>Karlon</h1><p>{msg}</p>
<input type="password" name="password" autocomplete="current-password" autofocus required>
<input type="hidden" name="next" value="{html.escape(next_path)}">
<button type="submit">Unlock</button></form></body></html>""", status_code=401 if wrong else 200)


class KarlonAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (PASSWORD and scope["type"] == "http" and scope.get("method") == "GET"
                and _gated(scope.get("path", "")) and not _cookie_ok(scope)):
            path = scope.get("path", "/")
            qs = (scope.get("query_string") or b"").decode("latin-1")
            await _login_page(path + (f"?{qs}" if qs else ""))(scope, receive, send)
            return
        await self.app(scope, receive, send)


router = APIRouter(tags=["auth"])


def _safe_next(n: str) -> str:
    # only same-site relative paths (no open redirect)
    return n if n.startswith("/") and not n.startswith("//") else "/"


@router.post("/login")
def login(password: str = Form(""), next: str = Form("/")):
    nxt = _safe_next(next)
    if not PASSWORD:
        return RedirectResponse(nxt, status_code=303)
    if not hmac.compare_digest(password.encode(), PASSWORD.encode()):
        return _login_page(nxt, wrong=True)
    resp = RedirectResponse(nxt, status_code=303)
    resp.headers.append(
        "set-cookie",
        f"{COOKIE}={_token()}; Max-Age={MAX_AGE}; Path=/; HttpOnly; Secure; SameSite=None; Partitioned",
    )
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.headers.append(
        "set-cookie", f"{COOKIE}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=None; Partitioned")
    return resp


@router.get("/api/auth/check")
def auth_check():
    """Kept so an app build that calls it keeps working. APIs are not gated."""
    return {"ok": True, "password_required": False}
