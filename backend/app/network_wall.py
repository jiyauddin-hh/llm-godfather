"""A second, read-only HTTP server that publishes the live wall to the LAN.

The dashboard itself stays on loopback because it runs arbitrary shell commands
with no authentication. This server exists so the *wall* can be watched from a
phone or another machine without exposing any of that: its entire surface is
one page and four JSON endpoints — the tiles, the Analytics figures and the
ticker's news and log tapes — all read-only. There is no route here that can start, stop,
edit or delete anything.

Tile URLs are rewritten from 127.0.0.1 to the machine's LAN address, because a
viewer's browser resolves them itself — loopback on a phone is the phone.
"""

import asyncio
import socket
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from .database import db
from .livereload import live_reload
from .network import lan_address
from .news import news
from .ports import non_loopback_ports
from .routers.traffic import state as traffic_state
from .service import runtime_for
from .wall import preview_url_for, probe_many

PAGE = Path(__file__).resolve().parent / "static" / "wall.html"

DEFAULT_PORT = 9200
# Bound on every interface so the page survives the machine's IP changing;
# reachability is what the firewall rule is for.
BIND_HOST = "0.0.0.0"


def _lan_reachable_ports() -> set[int]:
    """Ports another device can actually reach.

    Based on real listening addresses rather than our own share flag, since an
    app may bind 0.0.0.0 by itself and a preview URL may point at a different
    port than the one registered.
    """
    return non_loopback_ports()


def _to_lan(url: str | None, lan_ip: str) -> tuple[str | None, int | None]:
    """Rewrite a loopback preview URL to the LAN address; return (url, port)."""
    if not url:
        return None, None
    parsed = urlparse(url)
    port = parsed.port
    if parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
        netloc = f"{lan_ip}:{port}" if port else lan_ip
        parsed = parsed._replace(netloc=netloc)
    return urlunparse(parsed), port


def _tiles_for_network() -> list[dict]:
    lan = lan_address()
    if lan is None:
        return []
    lan_ip, _ = lan

    with db() as conn:
        rows = conn.execute("SELECT * FROM apps ORDER BY name COLLATE NOCASE").fetchall()

    reachable = _lan_reachable_ports()
    tiles: dict[int, dict] = {}
    to_probe: list[tuple[int, str]] = []

    for row in rows:
        local_url = preview_url_for(row["port"], row["preview_url"])
        lan_url, tile_port = _to_lan(local_url, lan_ip)
        status = runtime_for(row["id"], row["port"], bool(row["share_lan"])).status

        tile = {
            "app_id": row["id"],
            "name": row["name"],
            "port": row["port"],
            "url": lan_url,
            "status": status,
            "kind": "down",
            "note": None,
            # Live reload reaches this page through the data it already polls,
            # rather than one more route on a server that must stay read-only.
            "watching": bool(row["live_reload"]),
            "rev": live_reload.revision(row["id"]),
        }

        if lan_url is None:
            tile["kind"] = "noport"
            tile["note"] = "no port set"
        elif status not in {"running", "external"}:
            tile["kind"] = "down"
            tile["note"] = "not running"
        elif tile_port not in reachable:
            # Bound to loopback only, so no other device can load it.
            tile["kind"] = "unshared"
            tile["note"] = f"port {tile_port} is local-only — turn on local network access for it"
        else:
            to_probe.append((row["id"], local_url))

        tiles[row["id"]] = tile

    for app_id, result in probe_many(to_probe).items():
        tile = tiles[app_id]
        tile["kind"] = result.get("kind", "down")
        tile["note"] = result.get("note")

    return list(tiles.values())


def _build_app() -> FastAPI:
    viewer = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @viewer.get("/")
    def page():
        if not PAGE.is_file():
            return JSONResponse({"detail": "wall page missing"}, status_code=500)
        return FileResponse(PAGE, media_type="text/html")

    @viewer.get("/api/tiles")
    def tiles():
        return _tiles_for_network()

    @viewer.get("/api/traffic")
    def traffic(minutes: int = 60):
        # The same figures as the dashboard's Analytics tab. Reading them is
        # all this does: the "scan now" action stays on the loopback dashboard.
        return traffic_state(minutes).model_dump()

    @viewer.get("/api/news")
    def headlines(limit: int = 50, after: int = 0):
        # Polled rather than pushed: this server stays plain read-only GETs.
        return news.recent(limit, after)

    @viewer.get("/api/news/logs")
    def log_tape(limit: int = 40, after: int = 0):
        return news.lines(limit, after)

    @viewer.get("/healthz")
    def healthz():
        return {"status": "ok", "read_only": True}

    return viewer


class NetworkWall:
    """Owns the lifetime of the read-only wall server."""

    def __init__(self) -> None:
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None
        self.port: int | None = None

    @property
    def enabled(self) -> bool:
        return self._task is not None and not self._task.done()

    def url(self) -> str | None:
        lan = lan_address()
        if not self.enabled or lan is None or self.port is None:
            return None
        return f"http://{lan[0]}:{self.port}/"

    async def start(self, port: int) -> None:
        if self.enabled:
            await self.stop()

        # Bind ourselves so a port clash raises here, not inside the task.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((BIND_HOST, port))
        except OSError as exc:
            sock.close()
            raise RuntimeError(f"cannot use port {port}: {exc.strerror}") from exc
        sock.listen(128)
        sock.setblocking(False)

        config = uvicorn.Config(
            _build_app(), log_level="warning", access_log=False, lifespan="off"
        )
        server = uvicorn.Server(config)
        # We are not the main thread's owner here; leave signal handling alone.
        server.install_signal_handlers = lambda: None

        self._server = server
        self.port = port
        self._task = asyncio.create_task(server.serve(sockets=[sock]))

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=6)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self._task.cancel()
        self._server = None
        self._task = None
        self.port = None


network_wall = NetworkWall()
