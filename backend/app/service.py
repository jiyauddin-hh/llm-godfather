"""Shared helpers between routers: row lookup, serialisation and restarting."""

import json
import sqlite3
import time

from fastapi import HTTPException

from .database import db
from .manager import manager
from .models import AppOut, Runtime
from .network import is_own_relay, lan_address, reapply_relay, release_relay, shares
from .ports import port_is_listening, port_owner


def fetch_row(app_id: int) -> sqlite3.Row:
    with db() as conn:
        row = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"application {app_id} not found")
    return row


def share_fields(app_id: int, port: int | None, share_lan: bool) -> dict:
    """LAN sharing state for an app, cheap enough to include on every poll."""
    if not share_lan or not port:
        return {"lan_url": None, "share_active": False, "share_detail": None}

    lan = lan_address()
    if lan is None:
        return {"lan_url": None, "share_active": False, "share_detail": "no local network found"}

    ip, _ = lan
    relaying = shares.is_active(app_id)
    return {
        "lan_url": f"http://{ip}:{port}",
        "share_active": True,
        "share_detail": "relayed by God Father" if relaying else "app listens on all interfaces",
    }


def runtime_for(app_id: int, port: int | None, share_lan: bool = False) -> Runtime:
    """Live status, distinguishing processes we own from ones merely holding the port."""
    process = manager.peek(app_id)

    if process is not None:
        snapshot = process.snapshot(port)
    else:
        snapshot = {
            "status": "stopped",
            "pid": None,
            "started_at": None,
            "uptime_seconds": None,
            "exit_code": None,
            "restart_count": 0,
            "cpu_percent": None,
            "memory_mb": None,
            "port_open": port_is_listening(port) if port else None,
            "last_error": None,
        }

    # Someone is serving this port, but it is not a process we launched — so we
    # hold none of its output. Say so plainly instead of claiming it is stopped.
    if snapshot["status"] != "running" and snapshot.get("port_open"):
        snapshot["status"] = "external"
        owner = port_owner(port) if port else None
        if owner and owner.get("pid"):
            snapshot["pid"] = owner["pid"]

    snapshot.update(share_fields(app_id, port, share_lan))
    return Runtime(**snapshot)


def needs_of(row: sqlite3.Row) -> list[int]:
    """The app ids a row depends on. A malformed value means no dependencies."""
    try:
        value = json.loads(row["needs"] or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return [int(v) for v in value if isinstance(v, int) or (isinstance(v, str) and v.isdigit())]


def to_out(row: sqlite3.Row) -> AppOut:
    try:
        env = json.loads(row["env"])
    except (json.JSONDecodeError, TypeError):
        env = {}

    return AppOut(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        command=row["command"],
        working_dir=row["working_dir"],
        port=row["port"],
        env=env,
        auto_restart=bool(row["auto_restart"]),
        start_on_boot=bool(row["start_on_boot"]),
        share_lan=bool(row["share_lan"]),
        live_reload=bool(row["live_reload"]),
        preview_url=row["preview_url"],
        news_enabled=bool(row["news_enabled"]),
        news_keywords=row["news_keywords"],
        needs=needs_of(row),
        protected=bool(row["protected"]),
        uses_models=row["uses_models"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        runtime=runtime_for(row["id"], row["port"], bool(row["share_lan"])),
    )


# --------------------------------------------------------------- restarting

def port_conflict(port: int, owner: dict) -> HTTPException:
    who = owner.get("process") or "another process"
    pid = f" (pid {owner['pid']})" if owner.get("pid") else ""
    return HTTPException(
        status_code=409,
        detail=(
            f"Port {port} is already served by {who}{pid}. This app is running outside "
            f"God Father, so its logs cannot be captured. Stop it where it is running now, "
            f"or use Take over to stop it from here and start it under God Father."
        ),
    )


def clear_own_relay(app_id: int, port: int | None) -> dict | None:
    """Drop our LAN relay if it is what holds the port, then report the real owner."""
    if not port:
        return None
    owner = port_owner(port)
    if is_own_relay(owner):
        release_relay(app_id, manager.loop)
        time.sleep(0.3)
        owner = port_owner(port)
    return owner


def await_port_release(port: int, timeout: float = 3.0) -> None:
    """Give a port we just released a moment to actually close before rebinding."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not port_is_listening(port):
            return
        time.sleep(0.15)


def restart_process(app_id: int) -> int | None:
    """Stop an app we own and start it again, returning the port to wait on.

    Shared by the Restart button and by live reload so that an automatic
    restart is the same operation as a hand-pressed one — same relay handling,
    same port guard, same failure messages — rather than a second, subtly
    different implementation that drifts.
    """
    row = fetch_row(app_id)
    process = manager.get(app_id, row["name"])

    if process.is_alive:
        process.stop()
        if row["port"]:
            await_port_release(row["port"])

    # Stopping released whatever we owned, so anything still holding the port
    # belongs to someone else — launching now would only crash on bind.
    owner = clear_own_relay(app_id, row["port"])
    if owner:
        raise port_conflict(row["port"], owner)

    try:
        env = json.loads(row["env"])
    except (json.JSONDecodeError, TypeError):
        env = {}

    try:
        process.start(row["command"], row["working_dir"], env, bool(row["auto_restart"]))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if row["share_lan"] and row["port"]:
        time.sleep(1.0)
        reapply_relay(app_id, row["port"], manager.loop)

    return row["port"]
