"""crumb/endpoints.py — the catalogue of every endpoint, live and planned.

GET /api/endpoints returns it, the dashboard's Endpoints tab shows it, and
every planned endpoint already answers (501, with what it will do), so a
front end can be built against the contract before the feature exists.
To ship a planned one: implement the route in api.py and flip its status.
"""

from __future__ import annotations

LIVE, PLANNED = "live", "planned"

ENDPOINTS = [
    # ── the chat widget's contract (assistant-widget.js) ──
    {"method": "GET", "path": "/api/assistant", "status": LIVE, "group": "Chat",
     "summary": "Widget settings: name, greeting, suggestions, theme, Turnstile site key, ready."},
    {"method": "POST", "path": "/api/session", "status": LIVE, "group": "Chat",
     "summary": "Swap a Turnstile token for a chat-session cookie.", "body": {"token": "string"}},
    {"method": "POST", "path": "/api/chat", "status": LIVE, "group": "Chat",
     "summary": "Crumb's reply to the conversation so far.",
     "body": {"messages": [{"role": "user|assistant", "content": "string"}]}, "returns": {"text": "string"}},
    # ── the bakery, as data ──
    {"method": "GET", "path": "/api/menu", "status": LIVE, "group": "Bakery",
     "summary": "The counter: every bake with its number, name, price (rand) and description."},
    {"method": "GET", "path": "/api/story", "status": LIVE, "group": "Bakery",
     "summary": "The Sunday Crumb story, taglines and house rules."},
    {"method": "POST", "path": "/api/box/quote", "status": LIVE, "group": "Bakery",
     "summary": "Price a pastry box (0–12 of each treat). Demo: nothing is ordered.",
     "body": {"items": {"croissant": 2, "bun": 1}}},
    {"method": "POST", "path": "/api/feedback", "status": LIVE, "group": "Bakery",
     "summary": "Thumbs up/down on a reply, with an optional comment.",
     "body": {"rating": "up|down", "comment": "string?"}},
    # ── operations ──
    {"method": "GET", "path": "/api/health", "status": LIVE, "group": "Ops",
     "summary": "Up, version, model, whether the person check and the model are configured."},
    {"method": "GET", "path": "/api/endpoints", "status": LIVE, "group": "Ops", "summary": "This catalogue."},
    # ── planned: answer 501 with this description until built ──
    {"method": "GET", "path": "/api/specials", "status": PLANNED, "group": "Bakery",
     "summary": "Today's specials and seasonal bakes, set from the dashboard."},
    {"method": "GET", "path": "/api/hours", "status": PLANNED, "group": "Bakery",
     "summary": "Opening hours and holiday closures (for a real shop)."},
    {"method": "POST", "path": "/api/orders", "status": PLANNED, "group": "Orders",
     "summary": "Turn a pastry box into a pickup order.", "body": {"items": {"croissant": 2}, "pickup_at": "ISO time"}},
    {"method": "GET", "path": "/api/orders/{order_id}", "status": PLANNED, "group": "Orders",
     "summary": "An order's status: received, baking, ready, collected."},
    {"method": "POST", "path": "/api/payments/checkout", "status": PLANNED, "group": "Orders",
     "summary": "Start a payment for an order and return the checkout link."},
    {"method": "POST", "path": "/api/payments/webhook", "status": PLANNED, "group": "Orders",
     "summary": "Payment provider callback that marks an order paid."},
    {"method": "POST", "path": "/api/newsletter", "status": PLANNED, "group": "Club",
     "summary": "Join the Sunday Crumb Bakery Club mailing list (with consent).", "body": {"email": "string"}},
    {"method": "GET", "path": "/api/loyalty/{member_id}", "status": PLANNED, "group": "Club",
     "summary": "Bakery Club stamps and rewards."},
    {"method": "POST", "path": "/api/reservations", "status": PLANNED, "group": "Club",
     "summary": "Book the corner table or a bake-along class."},
    {"method": "GET", "path": "/api/chat/stream", "status": PLANNED, "group": "Chat",
     "summary": "Crumb's reply streamed word by word (Server-Sent Events)."},
    {"method": "POST", "path": "/api/handoff", "status": PLANNED, "group": "Chat",
     "summary": "Pass a conversation to a person by email or WhatsApp (the widget's handoff link)."},
]


def catalogue() -> dict:
    return {"live": sum(e["status"] == LIVE for e in ENDPOINTS),
            "planned": sum(e["status"] == PLANNED for e in ENDPOINTS),
            "endpoints": ENDPOINTS}


def planned() -> list[dict]:
    return [e for e in ENDPOINTS if e["status"] == PLANNED]
