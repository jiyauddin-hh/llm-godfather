"""God Father — FastAPI entrypoint."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .autostart import run_autostart
from .focus import recover as recover_focus
from .cloudflare import reconcile as reconcile_tunnels
from .ai_monitor import monitor as ai_monitor
from .system_monitor import monitor as system_monitor
from .tunnel_metrics import sampler as tunnel_sampler
from .traffic import monitor as traffic_monitor
from .config import BASE_DIR, CORS_ORIGINS
from .database import get_setting, init_db
from .database import db
from .livereload import live_reload
from .manager import manager
from .network import shares
from .news import news
from .network_wall import DEFAULT_PORT, network_wall
from .routers import (
    ai,
    apps,
    captures,
    cloudflare,
    discover,
    focus as focus_router,
    logs,
    network,
    news as news_router,
    ports,
    system,
    traffic,
    wall,
)
from .witness import witness
from . import witness as witness_store

UI_DIR = BASE_DIR.parent / "frontend" / "dist"


async def _restore_shares() -> None:
    """Re-open LAN relays for apps that were shared before the last restart.

    Deliberately runs *after* autostart. A relay bound before its app starts
    would occupy the address the app wants — and an app listening on 0.0.0.0
    cannot bind while we hold <lan-ip>:port — so God Father would lock its own
    applications out of their ports.
    """
    try:
        with db() as conn:
            rows = conn.execute(
                """SELECT id, port FROM apps
                   WHERE share_lan = 1 AND port IS NOT NULL AND start_on_boot = 0"""
            ).fetchall()
    except Exception:
        return
    for row in rows:
        try:
            await shares.enable(row["id"], row["port"])
        except Exception:
            continue


def _restore_watches() -> None:
    """Re-arm the file watcher for apps that had live reload switched on."""
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT id, name, working_dir FROM apps WHERE live_reload = 1"
            ).fetchall()
    except Exception:
        return
    for row in rows:
        live_reload.watch(row["id"], row["name"], row["working_dir"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Any capture still marked 'waiting for the camera' belongs to a previous
    # run of the old queue-and-shoot capture path; close those off.
    witness_store.reconcile()
    witness_store.seed_defaults()
    # Before the recording starts, and this ordering is the feature: after a
    # power cut every surviving frame is older than the retention window, so the
    # sweeper would delete the last thing the machine saw within seconds of boot.
    witness_store.recover()
    witness_store.mark_running()
    witness.start()
    loop = asyncio.get_running_loop()
    manager.bind_loop(loop)
    live_reload.bind_loop(loop)
    news.bind_loop(loop)
    await _restore_shares()
    # Before autostart: a step cut short by the restart is marked as such, and
    # autostart then reads which apps Focus Mode is keeping switched off.
    recover_focus()
    run_autostart()
    # After autostart, so the first scan sees the tree an app is about to serve.
    _restore_watches()
    # Watches only apps run elsewhere; its first pass records, it announces nothing.
    news.start()
    # Also after autostart: tunnel rows are ordinary applications with start on
    # boot set, so they are already coming up. This only squares the books.
    try:
        reconcile_tunnels()
    except Exception:
        pass
    # Counters only become a rate once two readings can be subtracted, so the
    # dashboard needs this running from boot rather than from first view.
    tunnel_sampler.start()
    # Reads the access lines apps already print; its first pass back-fills
    # from whatever the log files still hold.
    traffic_monitor.start()
    # Started last: its first reading walks the process table, and doing that
    # while autostart is still launching apps would measure the launch, not
    # the machine.
    system_monitor.start()
    # Last of all: its first pass reads every registered application's folder,
    # and doing that while autostart is still copying files into place would
    # scan a tree that is still moving.
    ai_monitor.start()
    if get_setting("network_wall_enabled", "0") == "1":
        try:
            await network_wall.start(int(get_setting("network_wall_port", str(DEFAULT_PORT))))
        except Exception:
            pass
    yield
    # First, while the recording still has frames to draw from — and it costs
    # nothing now, because the frame already exists rather than being taken.
    witness.record(None, "God Father", "shutdown", "God Father is shutting down")
    # Then say so, so the next boot knows this was an exit and not a power cut.
    witness_store.mark_clean_exit()
    witness.stop()
    live_reload.stop()
    news.stop()
    tunnel_sampler.stop()
    traffic_monitor.stop()
    ai_monitor.stop()
    system_monitor.stop()
    await network_wall.stop()
    await shares.disable_all()
    # Children live in their own sessions, so stop them explicitly
    # instead of leaving orphans with nobody capturing their output.
    manager.stop_all()


app = FastAPI(
    title="God Father",
    description="Manage your local applications, their logs and their ports.",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(apps.router)
app.include_router(logs.router)
app.include_router(ports.router)
app.include_router(discover.router)
app.include_router(network.router)
app.include_router(wall.router)
app.include_router(captures.router)
app.include_router(cloudflare.router)
app.include_router(system.router)
app.include_router(ai.router)
app.include_router(traffic.router)
app.include_router(news_router.router)
app.include_router(focus_router.router)


@app.get("/api/health", tags=["meta"])
def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "running": len(manager.running_ids()),
        "ui_built": UI_DIR.is_dir(),
    }


# Mounted last so it only catches paths no API route claimed. When the UI has
# not been built yet, say so instead of returning a bare 404.
if UI_DIR.is_dir():
    app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")
else:

    @app.get("/", include_in_schema=False)
    def ui_missing() -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "detail": "The web interface has not been built yet.",
                "fix": "cd frontend && npm install && npm run build",
                "api_docs": "/docs",
            },
        )
