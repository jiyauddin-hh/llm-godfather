"""Log retrieval — snapshot over REST, live tail over WebSocket."""

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..manager import manager
from ..models import LogLine
from ..service import fetch_row

router = APIRouter(prefix="/api/apps", tags=["logs"])


@router.get("/{app_id}/logs", response_model=list[LogLine])
def get_logs(app_id: int, limit: int = 500) -> list[LogLine]:
    row = fetch_row(app_id)
    process = manager.get(app_id, row["name"])
    return [LogLine(**line) for line in process.tail(max(1, min(limit, 5000)))]


@router.delete("/{app_id}/logs", status_code=204)
def clear_logs(app_id: int) -> None:
    row = fetch_row(app_id)
    manager.get(app_id, row["name"]).clear_logs()


@router.websocket("/{app_id}/logs/ws")
async def stream_logs(websocket: WebSocket, app_id: int) -> None:
    await websocket.accept()

    try:
        row = fetch_row(app_id)
    except Exception:
        await websocket.close(code=4004, reason="application not found")
        return

    process = manager.get(app_id, row["name"])
    queue = process.subscribe()

    try:
        # Replay recent history so the viewer is never empty on connect.
        for line in process.tail(300):
            await websocket.send_json(line)

        while True:
            try:
                line = await asyncio.wait_for(queue.get(), timeout=25)
                await websocket.send_json(line)
            except asyncio.TimeoutError:
                # Keeps proxies from dropping an idle connection.
                await websocket.send_json({"ts": "", "stream": "system", "text": "__ping__"})
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        pass
    finally:
        process.unsubscribe(queue)
