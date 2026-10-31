"""crumb/main.py — the Sunday Crumb Space: the site at /, Crumb's chat API
under /api, the WhatsApp bridge's endpoints (whatsapp.py), Crumb's kitchen
(the dashboard) at /admin, API docs at /docs.

Run: uvicorn crumb.main:app --host 0.0.0.0 --port 7860
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import admin, api, config, store, whatsapp


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init()
    yield


app = FastAPI(title="Sunday Crumb — Crumb assistant", version=config.VERSION, lifespan=lifespan)

# The site can also stay on its own host (e.g. the Cloudflare worker) and call
# this API across origins; cookies need credentials, so origins are listed.
app.add_middleware(CORSMiddleware, allow_origins=config.ALLOWED_ORIGINS, allow_credentials=True,
                   allow_methods=["GET", "POST", "PUT"], allow_headers=["Content-Type", "X-Crumb-Token"])

app.include_router(api.router)
app.include_router(admin.public)
app.include_router(admin.router)
app.include_router(whatsapp.router)

_site = config.STATIC / "site"
_admin = config.STATIC / "admin"
app.mount("/assistant", StaticFiles(directory=_site / "assistant"), name="assistant")
app.mount("/fonts", StaticFiles(directory=_site / "fonts"), name="fonts")
app.mount("/admin-assets", StaticFiles(directory=_admin), name="admin-assets")


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(_site / "index.html")


@app.get("/{name}", include_in_schema=False)
def site_file(name: str):
    """style.css, app.js, favicon.svg — only real files of the site."""
    path = (_site / name).resolve()
    if path.parent != _site.resolve() or not path.is_file():
        from fastapi import HTTPException
        raise HTTPException(404)
    return FileResponse(path)
