"""routers/outbox.py — outbox endpoints for wa_bridge delivery.

GET /api/outbox         — full pending queue (wa_status='pending')
                          used by the normal 5s poller in wa_bridge.
                          Returns all columns needed for text/file/image delivery.

GET /api/outbox/urgent  — dedicated queue for app-originated urgent customer
                          replies (wa_status='urgent'). wa_bridge polls this
                          at a tighter interval (e.g. 2 s) and processes these
                          BEFORE the normal /api/outbox items, so a guest
                          reply is never held up behind bulk/broadcast traffic.

                          This is the single, canonical implementation of the
                          urgent queue — a second copy used to live in
                          routers/customer_replies.py; that copy is gone, so
                          there is no more ambiguity about which one actually
                          runs. customer_replies.py owns only the write side
                          (POST /api/reply/{chat_id}).

                          Before returning rows, older still-urgent messages
                          in the same chat are marked wa_status='superseded'
                          so a burst of urgent replies to one chat doesn't
                          pile up and get redelivered on every poll — only
                          the single most-recent urgent message per chat is
                          ever handed to wa_bridge. Superseded rows are kept
                          for history, never (re-)delivered.

Messages land in the urgent queue via POST /api/reply/{chat_id}
(routers/customer_replies.py), stored as wa_status='urgent'. After DOM
delivery wa_bridge acks them via the standard:
    PATCH /api/chats/{chat_id}/messages/{msg_id}/wa-ack?status=sent
which removes them from this queue.
"""

from fastapi import APIRouter

from ..database import get_db

router = APIRouter(prefix="/api", tags=["outbox"])

# Shared SELECT so both endpoints return identical column shapes.
_OUTBOX_COLS = """
        SELECT  m.id,
                m.chat_id,
                m.sender,
                m.text,
                m.kind,
                m.media_url,
                m.created_at,
                m.direction,
                m.wa_status,
                c.wa_name,
                c.name AS chat_name
        FROM    messages m
        JOIN    chats c ON c.id = m.chat_id
"""


@router.get("/outbox")
def get_all_outbox():
    """
    Returns every message that:
      • was sent from the Karlon app or web UI (direction='out')
      • has not yet been delivered to WhatsApp (wa_status='pending')

    Each item includes wa_name (the raw WhatsApp chat name that wa_bridge
    uses to click the correct row in WA Web) and chat_name (the display
    name shown in the Karlon UI).
    """
    conn = get_db()
    rows = conn.execute(
        _OUTBOX_COLS +
        """
        WHERE   m.direction  = 'out'
          AND   m.wa_status  = 'pending'
        ORDER BY m.created_at ASC
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/outbox/urgent")
def get_urgent_outbox():
    """
    Dedicated queue for urgent app-originated customer replies.

    wa_bridge should poll this endpoint at a tighter interval (e.g. 2 s)
    and process these BEFORE the normal /api/outbox items so that guest
    replies are never held behind bulk or broadcast traffic.

    Messages land here via POST /api/reply/{chat_id} (routers/customer_replies.py),
    which stores them as wa_status='urgent'. After DOM delivery wa_bridge
    acks them via the standard:
        PATCH /api/chats/{chat_id}/messages/{msg_id}/wa-ack?status=sent
    which removes them from this queue.
    """
    conn = get_db()

    # Supersede logic (ported from the old duplicate implementation in
    # customer_replies.py): only the newest still-urgent message per chat
    # should ever be delivered. Mark anything older as 'superseded' so it's
    # kept for history but never handed to wa_bridge.
    conn.execute(
        """
        UPDATE messages
        SET wa_status = 'superseded'
        WHERE direction = 'out'
          AND wa_status = 'urgent'
          AND id NOT IN (
              SELECT m2.id
              FROM messages m2
              WHERE m2.direction = 'out'
                AND m2.wa_status = 'urgent'
                AND m2.chat_id = messages.chat_id
              ORDER BY m2.created_at DESC, m2.rowid DESC
              LIMIT 1
          )
        """
    )
    conn.commit()

    rows = conn.execute(
        _OUTBOX_COLS +
        """
        WHERE   m.direction  = 'out'
          AND   m.wa_status  = 'urgent'
        ORDER BY m.created_at ASC
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/outbox/documents")
def get_documents_outbox():
    """
    Dedicated queue for outbound documents — generated invoices and the static
    Terms & Conditions PDF (wa_status='documents').

    wa_bridge polls this BETWEEN the urgent queue and the normal /api/outbox
    queue, so a document is delivered ahead of bulk/broadcast traffic (a booking
    invoice shouldn't wait behind a 140-recipient blast) but still behind a live
    customer reply.

    Deliberately has NO supersede pass (unlike get_urgent_outbox): every document
    must be sent. Superseding would silently drop an invoice when a T&C is queued
    to the same chat right after it, or vice versa. After DOM delivery wa_bridge
    acks each one via the standard PATCH .../wa-ack?status=sent, which removes it
    from this queue.
    """
    conn = get_db()
    rows = conn.execute(
        _OUTBOX_COLS +
        """
        WHERE   m.direction  = 'out'
          AND   m.wa_status  = 'documents'
        ORDER BY m.created_at ASC
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]