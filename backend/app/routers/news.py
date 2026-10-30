"""The news ticker: what just happened, across every application."""

import asyncio

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from ..database import db
from ..models import LogTapeItem, NewsItem, NewsPost
from ..news import news

router = APIRouter(prefix="/api/news", tags=["news"])


@router.get("", response_model=list[NewsItem])
def recent(limit: int = 50, after: int = 0) -> list[NewsItem]:
    """Newest first. `after` returns only items newer than that id."""
    return [NewsItem(**item) for item in news.recent(limit, after)]


@router.post("", response_model=NewsItem, status_code=201)
def post(payload: NewsPost) -> NewsItem:
    """News from the application itself — the one source that knows what it means.

    For apps God Father runs, printing `[news] …` does the same without an HTTP
    client; this is for apps started elsewhere, whose output we never see.
    """
    key = str(payload.app).strip()
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM apps WHERE name = ? COLLATE NOCASE OR CAST(id AS TEXT) = ?", (key, key)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no application named '{key}'")

    item = news.for_app(row["id"], payload.level, "app", payload.message)
    if item is None:
        raise HTTPException(status_code=409, detail="this application is turned off on the news ticker")
    return NewsItem(**item)


@router.get("/logs", response_model=list[LogTapeItem])
def lines(limit: int = 40, after: int = 0) -> list[LogTapeItem]:
    """The All logs tape: the latest captured lines across every app, newest first."""
    return [LogTapeItem(**item) for item in news.lines(limit, after)]


@router.delete("", status_code=204)
def clear() -> None:
    news.clear()


@router.websocket("/ws")
async def stream(websocket: WebSocket) -> None:
    """One message per headline, the moment it happens."""
    await websocket.accept()
    queue = news.subscribe()
    try:
        await _relay(websocket, queue)
    finally:
        news.unsubscribe(queue)


@router.websocket("/logs/ws")
async def stream_lines(websocket: WebSocket) -> None:
    """Every captured line from every app, the moment it is written."""
    await websocket.accept()
    queue = news.subscribe_lines()
    try:
        await _relay(websocket, queue)
    finally:
        news.unsubscribe_lines(queue)


async def _relay(websocket: WebSocket, queue: asyncio.Queue) -> None:
    try:
        while True:
            try:
                await websocket.send_json(await asyncio.wait_for(queue.get(), timeout=25))
            except asyncio.TimeoutError:
                # Keeps an idle connection from being dropped underneath us.
                await websocket.send_json({"ping": True})
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        pass
