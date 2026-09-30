"""routers/chat_view.py — browser-based WhatsApp-style conversation view.

Served at GET /chat/{chat_id} as a full standalone HTML page.

Direction mapping (from the `direction` column stored by wa_bridge.py):
  direction = 'in'  → customer message  → LEFT  bubble  (white)
  direction = 'out' → staff/app reply   → RIGHT bubble  (green tint)

Direction is detected in the live WA DOM by wa_bridge.detect_direction():
  • CSS class  message-in  / message-out  on the container element
  • OR data-id attribute prefix: false_ = incoming, true_ = outgoing

This page connects to the existing WS pub/sub channel (/ws/{chat_id})
for real-time push of new messages the moment wa_bridge imports them or
a reply is sent from the Karlon app — no polling needed.
"""

import html
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from ..database import get_db

router = APIRouter(prefix="/chat", tags=["chat-view"])


def _e(s) -> str:
    """HTML-escape a value safely."""
    return html.escape(str(s or ""), quote=True)


def _bubble(m: dict) -> str:
    direction = m.get("direction") or "in"
    sender    = _e(m.get("sender") or "")
    text      = _e(m.get("text") or "").replace("\n", "<br>")
    ts        = _e((m.get("created_at") or "")[:16].replace("T", " "))
    kind      = m.get("kind") or "text"
    mid       = _e(m.get("id") or "")
    media_url = _e(m.get("media_url") or "")
    cls       = "out" if direction == "out" else "in"

    inner = f'<span class="name {cls}">{sender}</span>'

    if kind == "image" and media_url:
        inner += f'<img src="{media_url}" class="msg-img" alt="image">'
        if text:
            inner += f'<p class="msg-text">{text}</p>'
    elif kind in ("document", "pdf") and media_url:
        inner += (
            f'<a href="{media_url}" target="_blank" class="doc-link">'
            f'📄 {text or "document"}</a>'
        )
    else:
        inner += f'<p class="msg-text">{text}</p>'

    inner += f'<span class="ts">{ts}</span>'
    return (
        f'<div class="row {cls}" data-id="{mid}">'
        f'<div class="bubble {cls}">{inner}</div></div>'
    )


@router.get("/{chat_id}", response_class=HTMLResponse)
def chat_view(chat_id: str):
    conn = get_db()
    chat = conn.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
    if not chat:
        conn.close()
        raise HTTPException(404, "chat not found")

    rows = conn.execute(
        """SELECT id, sender, kind, text, media_url, created_at, direction, wa_status
           FROM messages
           WHERE chat_id = ?
           ORDER BY created_at ASC, rowid ASC""",
        (chat_id,),
    ).fetchall()
    conn.close()

    chat_name = _e(chat["name"] or chat["wa_name"] or chat_id)
    avatar    = _e(chat["avatar_emoji"] or "?")
    bubbles   = "\n".join(_bubble(dict(r)) for r in rows)
    chat_id_js = json.dumps(chat_id)

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{chat_name} — Karlon</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,'Segoe UI',Helvetica,sans-serif;
     background:#ECE5DD;height:100dvh;display:flex;flex-direction:column;overflow:hidden}}

/* ── header ── */
.header{{background:#075E54;color:#fff;padding:10px 14px;
         display:flex;align-items:center;gap:12px;flex-shrink:0;
         box-shadow:0 1px 3px rgba(0,0,0,.3)}}
.av{{width:42px;height:42px;border-radius:50%;background:#128C7E;
     display:flex;align-items:center;justify-content:center;
     font-size:17px;font-weight:700;color:#fff;flex-shrink:0}}
.hinfo h2{{font-size:16px;font-weight:600;line-height:1.2}}
.hinfo p{{font-size:12px;opacity:.75;margin-top:1px}}

/* ── chat area ── */
.chat{{flex:1;overflow-y:auto;padding:10px 8px;
       display:flex;flex-direction:column;gap:3px}}

/* ── rows ── */
.row{{display:flex;width:100%}}
.row.in {{justify-content:flex-start}}
.row.out{{justify-content:flex-end}}

/* ── bubbles ── */
.bubble{{max-width:68%;padding:6px 9px 4px;word-break:break-word;
         box-shadow:0 1px 1px rgba(0,0,0,.13);position:relative}}
.bubble.in {{background:#fff;border-radius:0 8px 8px 8px}}
.bubble.out{{background:#DCF8C6;border-radius:8px 0 8px 8px}}

/* bubble tail via pseudo-element */
.bubble.in::before{{
  content:'';position:absolute;top:0;left:-6px;
  border:6px solid transparent;border-right:6px solid #fff;border-top:0}}
.bubble.out::before{{
  content:'';position:absolute;top:0;right:-6px;
  border:6px solid transparent;border-left:6px solid #DCF8C6;border-top:0}}

/* ── bubble contents ── */
.name{{display:block;font-size:11px;font-weight:600;margin-bottom:3px}}
.name.in {{color:#00897B}}
.name.out{{color:#1565C0}}
.msg-text{{font-size:14px;line-height:1.45;color:#111;white-space:pre-wrap}}
.msg-img{{max-width:100%;border-radius:5px;margin:4px 0;display:block}}
.doc-link{{display:block;font-size:13px;color:#075E54;margin:4px 0;
           text-decoration:none;word-break:break-all}}
.doc-link:hover{{text-decoration:underline}}
.ts{{display:block;text-align:right;font-size:10px;color:#999;margin-top:3px}}
</style>
</head>
<body>
<div class="header">
  <div class="av">{avatar}</div>
  <div class="hinfo">
    <h2>{chat_name}</h2>
    <p id="status">connecting…</p>
  </div>
</div>
<div class="chat" id="chat">
{bubbles}
</div>

<script>
const CHAT_ID = {chat_id_js};
const chat    = document.getElementById('chat');
const status  = document.getElementById('status');
const seen    = new Set([...chat.querySelectorAll('.row')].map(r => r.dataset.id));

function scrollEnd() {{ chat.scrollTop = chat.scrollHeight; }}
scrollEnd();

function renderBubble(m) {{
  if (!m.id || seen.has(m.id)) return;
  seen.add(m.id);

  const dir  = m.direction || 'in';
  const cls  = dir === 'out' ? 'out' : 'in';
  const name = (m.sender || '').replace(/&/g,'&amp;').replace(/</g,'&lt;');
  const text = (m.text   || '').replace(/&/g,'&amp;').replace(/</g,'&lt;')
                                .replace(/\\n/g,'<br>');
  const ts   = (m.created_at || '').slice(0,16).replace('T',' ');

  let inner = `<span class="name ${{cls}}">${{name}}</span>`;

  if (m.kind === 'image' && m.media_url) {{
    inner += `<img src="${{m.media_url}}" class="msg-img" alt="image">`;
    if (text) inner += `<p class="msg-text">${{text}}</p>`;
  }} else if ((m.kind === 'document' || m.kind === 'pdf') && m.media_url) {{
    inner += `<a href="${{m.media_url}}" target="_blank" class="doc-link">📄 ${{text || 'document'}}</a>`;
  }} else {{
    inner += `<p class="msg-text">${{text}}</p>`;
  }}
  inner += `<span class="ts">${{ts}}</span>`;

  const row = document.createElement('div');
  row.className   = `row ${{cls}}`;
  row.dataset.id  = m.id;
  row.innerHTML   = `<div class="bubble ${{cls}}">${{inner}}</div>`;
  chat.appendChild(row);
  scrollEnd();
}}

// WebSocket real-time push
const proto = location.protocol === 'https:' ? 'wss' : 'ws';
let ws;
function connect() {{
  ws = new WebSocket(`${{proto}}://${{location.host}}/ws/${{CHAT_ID}}`);
  ws.onopen    = () => status.textContent = 'live ●';
  ws.onmessage = e => {{
    try {{
      const d = JSON.parse(e.data);
      if (d.type === 'message' && d.data) renderBubble(d.data);
    }} catch(_) {{}}
  }};
  ws.onclose = () => {{
    status.textContent = 'reconnecting…';
    setTimeout(connect, 3000);
  }};
  ws.onerror = () => ws.close();
}}
connect();
</script>
</body>
</html>"""
    return HTMLResponse(content=page)
