"""crumb/knowledge.py — what Crumb knows: Sunday Crumb's bakes, the pastry
box, the story and the house rules, taken from the site itself. Edit this file
to change the facts; the dashboard's "Kitchen notes" add to them at runtime.

Also the offline answerer: when no model is configured (or the model is down),
Crumb still answers the common questions from these facts.
"""

from __future__ import annotations

import re

BUSINESS = "Sunday Crumb"
NAME = "Crumb"
STUDIO = "Taggs Studio"

PRODUCTS = [
    {"id": "croissant", "number": "01", "name": "The butter half", "price": 32, "tag": "The forever favourite",
     "description": "A golden croissant. Shatteringly flaky. Best enjoyed with absolutely no rush."},
    {"id": "bun", "number": "02", "name": "Roll with it", "price": 38, "tag": "A little twisted",
     "description": "Soft cinnamon swirls, a sticky middle, and just enough vanilla icing."},
    {"id": "cookie", "number": "03", "name": "One smart cookie", "price": 26, "tag": "Big crumb energy",
     "description": "Crisp edges. A soft heart. Dark chocolate. A tiny pinch of sea salt to finish."},
]
CURRENCY = "ZAR"
BOX_MAX_EACH = 12

STORY = ("Imagine a bakery on your walk home. The window's a little foggy. Something cinnamon-y is happening. "
         "And there's always a corner to sit for a minute. That's Sunday Crumb: a made-up place built around a "
         "simple idea, that everyday things deserve a little extra care. Come for a croissant. Stay for the feeling.")
TAGLINES = ["A little butter. A lot of joy.", "Good things take their sweet time.", "Less hurry. More butter.",
            "Life's short. Get the pastry."]

RULES = [
    "Sunday Crumb is a fictional local-business concept by Taggs Studio (Concept 001). There is no real shop, "
    "no opening hours, no delivery, no address and no real orders.",
    "Prices are illustrative, in South African rand (R).",
    "The pastry box on the page is a playful demo: pick up to 12 of each treat and it adds up the total. There is "
    "no checkout, payment or personal details.",
]

DEFAULT_SETTINGS = {
    "name": NAME,
    "business": BUSINESS,
    "greeting": "Hi, I’m Crumb. Ask me about the bakes, the pastry box or the story behind Sunday Crumb.",
    "notice": "Concept demo. Messages go to an AI service to write replies, so please don’t share personal details.",
    "suggestions": ["What should I try first?", "How does the pastry box work?", "Are you open on Sunday?"],
    "theme": {"accent": "#a52a20", "ink": "#46251f", "paper": "#fffaf0", "soft": "#f9edbe"},
    "ready": True,
    "notes": "",                 # kitchen notes from the dashboard, added to what Crumb knows
    "wa_auto_reply": True,       # Crumb answers WhatsApp messages (through the PC bridge)
}


def menu_text() -> str:
    return "\n".join(f"- {p['name']} (#{p['number']}, {p['tag']}): R{p['price']}. {p['description']}"
                     for p in PRODUCTS)


CHANNELS = {
    "web": ("You chat with visitors of the bakery's website.",
            "You may use short Markdown lists or **bold**."),
    "whatsapp": ("You reply to people who message the bakery on WhatsApp.",
                 "Use WhatsApp formatting only: *bold* with single asterisks, plain dashes for lists, "
                 "no headings, tables or Markdown links."),
}


def system_prompt(notes: str = "", channel: str = "web") -> str:
    """Crumb's instructions. Stable between requests (only the dashboard's
    notes change it), so it caches; one version per channel."""
    where, formatting = CHANNELS.get(channel, CHANNELS["web"])
    extra = f"\nKitchen notes from the team (true, use them):\n{notes.strip()}\n" if notes.strip() else ""
    return (
        f"You are {NAME}, the friendly assistant of {BUSINESS}, an imaginary neighbourhood bakery made by "
        f"{STUDIO}. {where}\n\n"
        f"Voice: warm, playful, short. Two to four sentences, at most about 80 words. Plain words, a light "
        f"bakery pun now and then, at most one emoji. {formatting}\n\n"
        f"The counter (illustrative prices in rand):\n{menu_text()}\n\n"
        f"The story: {STORY}\nTaglines: {' · '.join(TAGLINES)}\n\n"
        f"House rules:\n" + "\n".join(f"- {r}" for r in RULES) + "\n" + extra +
        "\nHow to answer:\n"
        "- Answer only from the facts above. If something isn't covered (ingredients, allergens, hours, "
        "location, delivery), say kindly that Sunday Crumb is make-believe, so that detail doesn't exist, and "
        "suggest something on the page instead.\n"
        "- Never invent new products, prices, discounts, opening hours or addresses.\n"
        "- Never ask for or accept personal details (names, phone numbers, emails, addresses, payment). If a "
        "visitor shares some, don't repeat it and remind them gently not to.\n"
        "- For anything unrelated to the bakery, steer back to the bakes, the box or the story in one friendly line.\n"
        "- You can't place orders or take payments: the box is a demo."
    )


# ── offline answers ─────────────────────────────────────────────────────────

def _has(text: str, *words: str) -> bool:
    return any(re.search(r"(?<![a-z])" + w + r"(?![a-z])", text) for w in words)


def offline_answer(question: str) -> str:
    """A short, correct answer from the facts, no model needed."""
    t = (question or "").lower()
    if _has(t, "open", "hours", "closed", "time", "address", "where", "location", "deliver(y)?"):
        return ("Sunday Crumb is a make-believe bakery, so it's open every imaginary morning and nowhere on a "
                "map 🥐 No real hours, address or delivery. You can still build a dream box on the page!")
    if _has(t, "box", "order", "basket", "checkout", "buy", "pay(ment)?"):
        return ("The pastry box is a playful demo: add up to 12 of each treat from the counter and it does the "
                "very important crumb maths for you. No checkout, payment or personal details needed.")
    if _has(t, "price", "cost", "how much", "rand"):
        return "Illustrative prices: " + "; ".join(f"**{p['name']}** R{p['price']}" for p in PRODUCTS) + "."
    if _has(t, "croissant", "butter"):
        p = PRODUCTS[0]
        return f"**{p['name']}** (R{p['price']}): {p['description']}"
    if _has(t, "cinnamon", "bun", "roll", "swirl"):
        p = PRODUCTS[1]
        return f"**{p['name']}** (R{p['price']}): {p['description']}"
    if _has(t, "cookie", "chocolate"):
        p = PRODUCTS[2]
        return f"**{p['name']}** (R{p['price']}): {p['description']}"
    if _has(t, "story", "who", "about", "taggs", "real", "fake", "fictional"):
        return f"{STORY} It's a concept by {STUDIO}."
    if _has(t, "try", "recommend", "best", "favourite", "favorite", "first", "menu", "bakes?", "sell"):
        return ("Start with **The butter half**, our forever-favourite croissant (R32). Then it's a tie between "
                "the sticky cinnamon **Roll with it** (R38) and **One smart cookie** (R26). Why not all three?")
    return ("I'm best at bakes, the pastry box and the Sunday Crumb story. Ask me what to try first, or how the "
            "box works!")


def box_quote(items: dict) -> dict:
    """The pastry box total, the same rules as the page (0–12 of each)."""
    by_id = {p["id"]: p for p in PRODUCTS}
    lines, total, errors = [], 0, []
    for pid, qty in (items or {}).items():
        p = by_id.get(pid)
        if not p:
            errors.append(f"unknown treat '{pid}'")
            continue
        if not isinstance(qty, int) or qty < 0 or qty > BOX_MAX_EACH:
            errors.append(f"{p['name']}: 0 to {BOX_MAX_EACH}")
            continue
        if qty:
            lines.append({"id": pid, "name": p["name"], "quantity": qty, "each": p["price"], "subtotal": qty * p["price"]})
            total += qty * p["price"]
    return {"items": lines, "count": sum(line["quantity"] for line in lines), "total": total,
            "currency": CURRENCY, "errors": errors, "demo": True}
