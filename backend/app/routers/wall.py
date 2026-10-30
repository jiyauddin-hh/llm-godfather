"""The live wall: every app that can be shown in a frame, probed and listed."""

import asyncio

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from ..database import db, get_setting, set_setting
from ..livereload import live_reload
from ..models import NetworkWallState, NetworkWallToggle, WallTile
from ..network import lan_address, ufw_available, ufw_can_manage, ufw_command_text, ufw_open
from ..network_wall import DEFAULT_PORT, network_wall
from ..service import runtime_for
from ..wall import preview_url_for, probe, probe_many

router = APIRouter(prefix="/api/wall", tags=["wall"])

SETTING_ENABLED = "network_wall_enabled"
SETTING_PORT = "network_wall_port"


def _state() -> NetworkWallState:
    lan = lan_address()
    port = network_wall.port or int(get_setting(SETTING_PORT, str(DEFAULT_PORT)) or DEFAULT_PORT)
    managed = ufw_available() and ufw_can_manage()
    return NetworkWallState(
        enabled=network_wall.enabled,
        port=port,
        url=network_wall.url(),
        lan_ip=lan[0] if lan else None,
        firewall_managed=managed,
        manual_command=None if managed or not lan else ufw_command_text(port, lan[1]),
    )


@router.get("/network", response_model=NetworkWallState)
def network_state() -> NetworkWallState:
    return _state()


@router.post("/network", response_model=NetworkWallState)
async def set_network(payload: NetworkWallToggle) -> NetworkWallState:
    """Publish the wall to the LAN, read-only, on its own port."""
    if not payload.enabled:
        await network_wall.stop()
        set_setting(SETTING_ENABLED, "0")
        return _state()

    lan = lan_address()
    if lan is None:
        raise HTTPException(status_code=400, detail="No local network interface was found.")

    port = payload.port or int(get_setting(SETTING_PORT, str(DEFAULT_PORT)) or DEFAULT_PORT)
    try:
        await network_wall.start(port)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    set_setting(SETTING_ENABLED, "1")
    set_setting(SETTING_PORT, str(port))

    if ufw_available() and ufw_can_manage():
        ufw_open(port, lan[1])

    return _state()


@router.get("", response_model=list[WallTile])
def wall() -> list[WallTile]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM apps ORDER BY name COLLATE NOCASE").fetchall()

    tiles: dict[int, WallTile] = {}
    to_probe: list[tuple[int, str]] = []

    for row in rows:
        runtime = runtime_for(row["id"], row["port"], bool(row["share_lan"]))
        url = preview_url_for(row["port"], row["preview_url"])

        tile = WallTile(
            app_id=row["id"],
            name=row["name"],
            port=row["port"],
            url=url,
            status=runtime.status,
            watching=bool(row["live_reload"]),
            rev=live_reload.revision(row["id"]),
        )

        if url is None:
            tile.kind = "noport"
            tile.note = "no port or preview URL set"
        elif runtime.status in {"running", "external"}:
            to_probe.append((row["id"], url))
        else:
            tile.kind = "down"
            tile.note = "not running"

        tiles[row["id"]] = tile

    for app_id, result in probe_many(to_probe).items():
        tile = tiles[app_id]
        tile.kind = result.get("kind", "down")
        tile.note = result.get("note")
        tile.http_status = result.get("http_status")
        tile.content_type = result.get("content_type")

    return list(tiles.values())


@router.get("/{app_id}/probe", response_model=WallTile)
def probe_one(app_id: int) -> WallTile:
    """Re-check a single tile without re-probing the whole wall."""
    with db() as conn:
        row = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()

    runtime = runtime_for(row["id"], row["port"], bool(row["share_lan"]))
    url = preview_url_for(row["port"], row["preview_url"])
    tile = WallTile(
        app_id=row["id"],
        name=row["name"],
        port=row["port"],
        url=url,
        status=runtime.status,
        watching=bool(row["live_reload"]),
        rev=live_reload.revision(row["id"]),
    )

    if url is None:
        tile.kind = "noport"
        tile.note = "no port or preview URL set"
        return tile

    result = probe(url)
    tile.kind = result.get("kind", "down")
    tile.note = result.get("note")
    tile.http_status = result.get("http_status")
    tile.content_type = result.get("content_type")
    return tile


@router.websocket("/ws")
async def watch_changes(websocket: WebSocket) -> None:
    """Push a tile's new revision the moment its files change.

    The wall already re-probes on a timer, but a timer is the wrong shape for
    an edit: fifteen seconds of staring at the old page is exactly the problem
    live reload exists to remove. One message per change, carrying only which
    tile moved and why, so the page can reload that frame and leave the others
    holding their state.
    """
    await websocket.accept()
    queue = live_reload.subscribe()

    try:
        while True:
            try:
                await websocket.send_json(await asyncio.wait_for(queue.get(), timeout=25))
            except asyncio.TimeoutError:
                # Keeps an idle connection from being dropped underneath us.
                await websocket.send_json({"app_id": 0, "rev": 0, "ping": True})
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        pass
    finally:
        live_reload.unsubscribe(queue)
