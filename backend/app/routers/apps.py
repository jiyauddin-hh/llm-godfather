"""CRUD and lifecycle control for registered applications."""

import json
import sqlite3
import time

from fastapi import APIRouter, HTTPException

from ..database import db, record_event, utcnow
from ..livereload import live_reload
from ..manager import manager
from ..news import news
from ..models import ActionResult, AppCreate, AppOut, AppUpdate, EventOut, Occupant, Stats
from ..network import reapply_relay
from ..ports import one_scan, port_is_listening
from ..service import (
    clear_own_relay,
    fetch_row,
    port_conflict,
    restart_process,
    runtime_for,
    to_out,
)
from ..takeover import inspect as inspect_port_owner
from ..takeover import reclaim
from ..witness import witness

router = APIRouter(prefix="/api/apps", tags=["apps"])


def _valid_needs(app_id: int | None, needs: list[int]) -> list[int]:
    """Dependencies that name real apps, without the app itself or repeats."""
    wanted = [n for n in dict.fromkeys(needs) if n != app_id]
    if not wanted:
        return []
    with db() as conn:
        found = {
            row["id"]
            for row in conn.execute(
                f"SELECT id FROM apps WHERE id IN ({','.join('?' * len(wanted))})", wanted
            )
        }
    missing = [n for n in wanted if n not in found]
    if missing:
        raise HTTPException(status_code=400, detail=f"needs: no application with id {missing[0]}")
    return wanted


def _clean_models(text: str) -> str:
    """'a, b,,c ' -> 'a, b, c'."""
    return ", ".join(part.strip() for part in text.split(",") if part.strip())


def _sync_watch(row) -> None:
    """Keep the file watcher in step with what the row now says."""
    if row["live_reload"]:
        live_reload.watch(row["id"], row["name"], row["working_dir"])
    else:
        live_reload.unwatch(row["id"])


@router.get("", response_model=list[AppOut])
def list_apps() -> list[AppOut]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM apps ORDER BY name COLLATE NOCASE").fetchall()
    with one_scan():
        return [to_out(row) for row in rows]


@router.post("", response_model=AppOut, status_code=201)
def create_app(payload: AppCreate) -> AppOut:
    now = utcnow()
    try:
        with db() as conn:
            cursor = conn.execute(
                """INSERT INTO apps (name, description, command, working_dir, port, env,
                                     auto_restart, start_on_boot, share_lan, live_reload,
                                     preview_url, news_enabled, news_keywords,
                                     needs, protected, uses_models,
                                     created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    payload.name,
                    payload.description,
                    payload.command,
                    payload.working_dir,
                    payload.port,
                    json.dumps(payload.env),
                    int(payload.auto_restart),
                    int(payload.start_on_boot),
                    int(payload.share_lan),
                    int(payload.live_reload),
                    payload.preview_url,
                    int(payload.news_enabled),
                    payload.news_keywords.strip(),
                    json.dumps(_valid_needs(None, payload.needs)),
                    int(payload.protected),
                    _clean_models(payload.uses_models),
                    now,
                    now,
                ),
            )
            new_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail=f"an application named '{payload.name}' already exists")

    news.invalidate()
    record_event(new_id, "created", payload.name)
    row = fetch_row(new_id)
    _sync_watch(row)
    return to_out(row)


@router.get("/stats", response_model=Stats)
def stats() -> Stats:
    with db() as conn:
        rows = conn.execute("SELECT id, port FROM apps").fetchall()

    counts = {"running": 0, "crashed": 0, "stopped": 0, "external": 0}
    ports_in_use = 0
    with one_scan():
        for row in rows:
            status = runtime_for(row["id"], row["port"]).status
            counts[status if status in counts else "stopped"] += 1
            if row["port"] and port_is_listening(row["port"]):
                ports_in_use += 1

    return Stats(
        total=len(rows),
        running=counts["running"],
        stopped=counts["stopped"],
        crashed=counts["crashed"],
        external=counts["external"],
        ports_in_use=ports_in_use,
    )


@router.get("/{app_id}", response_model=AppOut)
def get_app(app_id: int) -> AppOut:
    return to_out(fetch_row(app_id))


@router.put("/{app_id}", response_model=AppOut)
def update_app(app_id: int, payload: AppUpdate) -> AppOut:
    fetch_row(app_id)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return to_out(fetch_row(app_id))

    if "env" in changes:
        changes["env"] = json.dumps(changes["env"])
    if "needs" in changes:
        changes["needs"] = json.dumps(_valid_needs(app_id, changes["needs"] or []))
    if "uses_models" in changes:
        changes["uses_models"] = _clean_models(changes["uses_models"] or "")
    for flag in ("auto_restart", "start_on_boot", "share_lan", "live_reload", "news_enabled", "protected"):
        if flag in changes:
            changes[flag] = int(changes[flag])

    changes["updated_at"] = utcnow()
    assignments = ", ".join(f"{column} = ?" for column in changes)

    try:
        with db() as conn:
            conn.execute(f"UPDATE apps SET {assignments} WHERE id = ?", (*changes.values(), app_id))
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="another application already uses that name")

    news.invalidate()
    record_event(app_id, "updated", ", ".join(changes.keys()))
    row = fetch_row(app_id)
    # The flag may have been turned off, or the directory moved out from under
    # an existing watch, so re-derive it from the row rather than the payload.
    _sync_watch(row)
    return to_out(row)


@router.delete("/{app_id}", status_code=204)
def delete_app(app_id: int) -> None:
    fetch_row(app_id)
    live_reload.unwatch(app_id)
    manager.forget(app_id)
    with db() as conn:
        conn.execute("DELETE FROM apps WHERE id = ?", (app_id,))
    news.forget(app_id)


@router.post("/{app_id}/start", response_model=ActionResult)
def start_app(app_id: int) -> ActionResult:
    row = fetch_row(app_id)
    process = manager.get(app_id, row["name"])

    if process.is_alive:
        raise HTTPException(status_code=409, detail=f"'{row['name']}' is already running")

    # Warn early rather than letting the child die with a cryptic bind error.
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

    return ActionResult(ok=True, message=f"{row['name']} started", runtime=runtime_for(row["id"], row["port"]))


@router.post("/{app_id}/stop", response_model=ActionResult)
def stop_app(app_id: int) -> ActionResult:
    row = fetch_row(app_id)
    process = manager.get(app_id, row["name"])

    if not process.is_alive:
        return ActionResult(
            ok=True,
            message=f"{row['name']} was not running",
            runtime=runtime_for(row["id"], row["port"]),
        )

    process.stop()
    return ActionResult(ok=True, message=f"{row['name']} stopped", runtime=runtime_for(row["id"], row["port"]))


@router.post("/{app_id}/restart", response_model=ActionResult)
def restart_app(app_id: int) -> ActionResult:
    row = fetch_row(app_id)
    restart_process(app_id)
    record_event(app_id, "restarted", "manual")
    return ActionResult(ok=True, message=f"{row['name']} restarted", runtime=runtime_for(row["id"], row["port"]))


@router.get("/{app_id}/occupant", response_model=Occupant)
def app_occupant(app_id: int) -> Occupant:
    """Who holds this app's port right now, and whether Take over can stop it."""
    row = fetch_row(app_id)
    if not row["port"]:
        return Occupant(occupied=False)
    clear_own_relay(app_id, row["port"])
    return Occupant(**inspect_port_owner(row["port"]))


@router.post("/{app_id}/takeover", response_model=ActionResult)
def takeover_app(app_id: int) -> ActionResult:
    """Stop whatever holds the port, then start this app under God Father."""
    row = fetch_row(app_id)
    process = manager.get(app_id, row["name"])

    if process.is_alive:
        raise HTTPException(status_code=409, detail=f"'{row['name']}' is already running here")
    if not row["port"]:
        raise HTTPException(status_code=400, detail="This application has no port, so there is nothing to take over.")

    clear_own_relay(app_id, row["port"])

    freed, detail = reclaim(row["port"])
    if not freed:
        raise HTTPException(status_code=409, detail=detail)

    process.emit("system", f"take over: {detail}")
    record_event(app_id, "takeover", detail)
    witness.record(app_id, row["name"], "takeover", detail)

    try:
        env = json.loads(row["env"])
    except (json.JSONDecodeError, TypeError):
        env = {}

    # Client connections the old process served leave TIME-WAIT sockets on the
    # port. An app without SO_REUSEADDR cannot bind through those, so give it
    # one more go rather than reporting a failure the user cannot act on.
    for attempt in range(2):
        try:
            process.start(row["command"], row["working_dir"], env, bool(row["auto_restart"]))
            break
        except RuntimeError as exc:
            if attempt == 1:
                raise HTTPException(status_code=400, detail=f"{detail}, but starting failed: {exc}")
            time.sleep(2.0)

    if row["share_lan"] and row["port"]:
        time.sleep(1.0)
        reapply_relay(app_id, row["port"], manager.loop)

    return ActionResult(
        ok=True,
        message=f"Took over port {row['port']} and started {row['name']}",
        runtime=runtime_for(row["id"], row["port"]),
    )


@router.get("/{app_id}/events", response_model=list[EventOut])
def app_events(app_id: int, limit: int = 100) -> list[EventOut]:
    fetch_row(app_id)
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE app_id = ? ORDER BY id DESC LIMIT ?",
            (app_id, max(1, min(limit, 500))),
        ).fetchall()
    return [EventOut(**dict(row)) for row in rows]
