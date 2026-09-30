"""ws_manager.py — tracks open WebSocket connections per chat and
broadcasts messages/poll updates to them.

RECONSTRUCTED: the original file failed to extract (0 bytes). Rebuilt from
usage across every router that DID extract cleanly — all of them call
`await manager.broadcast(chat_id, {"type": ..., "data": ...})` after an
insert, and routers/websocket.py (also reconstructed) needs `connect` /
`disconnect`.

Note: per the Android app's ChatWebSocketClient.kt doc comment, the app
currently polls REST instead of using this — so this class is exercised
only if/when you re-wire the client back to WebSocket push updates.
"""

from collections import defaultdict
from typing import Any

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, list[WebSocket]] = defaultdict(list)

    async def connect(self, chat_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections[chat_id].append(websocket)

    def disconnect(self, chat_id: str, websocket: WebSocket) -> None:
        conns = self._connections.get(chat_id)
        if conns and websocket in conns:
            conns.remove(websocket)
        if conns is not None and not conns:
            self._connections.pop(chat_id, None)

    async def broadcast(self, chat_id: str, payload: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in self._connections.get(chat_id, []):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(chat_id, ws)


manager = ConnectionManager()
