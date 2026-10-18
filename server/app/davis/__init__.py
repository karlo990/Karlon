"""Davis — KarlCon Elite Retreats' WhatsApp reception and booking assistant.

Routers call these two hooks; they import the engine lazily so importing a
router never pulls in the engine (and the engine imports routers)."""


def notify_inbound(chat_id: str) -> None:
    """A guest message landed in this chat (bridge sync / import)."""
    try:
        from .engine import engine
        engine.notify_inbound(chat_id)
    except Exception as e:          # Davis must never break message import
        print(f"[davis] notify failed for {chat_id}: {e}")


def notify_refresh_done(job_id: int, early: bool = False) -> None:
    """The scraper finished a (city, dates) request, or (early=True) has
    priced the first few homes for it: guests waiting can see them now."""
    try:
        from .engine import engine
        engine.notify_refresh_done(job_id, early)
    except Exception as e:
        print(f"[davis] refresh hook failed for job {job_id}: {e}")
