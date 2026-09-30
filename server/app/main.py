from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from .config import ALLOWED_ORIGINS, STATIC_DIR
from .database import init_db
from .routers import broadcast, chats, messages, polls, websocket
from .routers import outbox          # global outbox for wa_bridge
from .routers import team_location   # live team location tracking
from .routers import invoices        # guest invoice generation
from .routers import houses          # scraper -> server -> app "available houses now"
from .routers import house_refresh   # on-demand re-price for exact dates (audit §3)
from .routers import customer_replies  # dedicated urgent-reply line (app → guest WA)
from .routers import chat_view       # customer-facing chat view
from .routers import chat_sync       # one chat snapshot per sync (wa_bridge)
from . import auth                   # KARLON_PASSWORD (HF Space secret)

init_db()
app = FastAPI(title="Karlon")
# Password gate for the HTML pages only ("/" and "/chat/..."); APIs stay open.
app.add_middleware(auth.KarlonAuthMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.include_router(auth.router)

app.include_router(chat_sync.router)          # before chats/messages: /repair-names, /{id}/sync
app.include_router(chats.router)
app.include_router(messages.router)
app.include_router(polls.router)
app.include_router(broadcast.router)
app.include_router(websocket.router)
app.include_router(outbox.router)
app.include_router(team_location.router)
app.include_router(invoices.router)
app.include_router(houses.router)
app.include_router(house_refresh.router)
app.include_router(customer_replies.router)   # POST /api/reply/{chat_id}
app.include_router(chat_view.router)

@app.get("/api/health")
def health(): return {"status": "ok", "service": "karlon"}

@app.get("/", response_class=HTMLResponse)
def index(): return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")