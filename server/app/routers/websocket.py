"""routers/websocket.py — /ws/{chat_id} endpoint.

RECONSTRUCTED: the original file failed to extract (0 bytes). Rebuilt to
match what ChatWebSocketClient.kt (Android side) connects to:
ws(s)://<server>/ws/{chatId} — and to use the reconstructed ws_manager.

Currently unused by the Android app (it polls REST instead — see that
file's doc comment) but kept wired up here in case you switch back.
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..ws_manager import manager

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/{chat_id}")
async def chat_socket(websocket: WebSocket, chat_id: str):
    await manager.connect(chat_id, websocket)
    try:
        while True:
            # Clients don't need to send anything; this just keeps the
            # connection open and lets us detect disconnects.
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(chat_id, websocket)
