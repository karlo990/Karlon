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


def notify_refresh_done(job_id: int) -> None:
    """The scraper finished re-pricing a (city, dates) window."""
    try:
        from .engine import engine
        engine.notify_refresh_done(job_id)
    except Exception as e:
        print(f"[davis] refresh hook failed for job {job_id}: {e}")
