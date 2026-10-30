"""Publish local ports to the internet through Cloudflare Tunnel."""

import sqlite3

from fastapi import APIRouter, HTTPException

from .. import cloudflare as cf
from .. import oauth
from .. import tunnel_metrics as tm
from ..database import db, utcnow
from ..manager import manager
from ..models import (
    TunnelMetrics,
    TunnelNameRequest,
    TunnelPoint,
    TunnelRoute,
    TunnelRouteCreate,
    TunnelRouteMetrics,
    TunnelRouteUpdate,
    TunnelStatus,
    TunnelToken,
)
from ..ports import one_scan, port_is_listening

router = APIRouter(prefix="/api/cloudflare", tags=["cloudflare"])

INSTALL_HINT = (
    "cloudflared was not found. Install it, then reload this tab — on Debian and "
    "Ubuntu: curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/"
    "cloudflared-linux-amd64 -o ~/.local/bin/cloudflared && chmod +x ~/.local/bin/cloudflared"
)

LOGIN_HINT = (
    "Run  cloudflared tunnel login  in a terminal and pick your domain. It opens a "
    "browser, so it cannot be done from this page. Until then, routes go live on "
    "temporary trycloudflare.com addresses."
)


def _app_index() -> dict[int, dict]:
    """Registered apps keyed by port, so a route can name what it is serving."""
    with db() as conn:
        rows = conn.execute("SELECT id, name, port FROM apps WHERE port IS NOT NULL").fetchall()
    return {row["port"]: {"id": row["id"], "name": row["name"]} for row in rows}


def _app_row(app_id: int | None) -> dict | None:
    if not app_id:
        return None
    with db() as conn:
        row = conn.execute("SELECT id, name FROM apps WHERE id = ?", (app_id,)).fetchone()
    return dict(row) if row else None


def _to_out(item: dict, serving: dict[int, dict]) -> TunnelRoute:
    runner = _app_row(item["app_id"])

    status = "stopped"
    if runner:
        process = manager.peek(runner["id"])
        if process is not None:
            status = process.snapshot(item["metrics_port"])["status"]

    owner = serving.get(item["port"])

    return TunnelRoute(
        id=item["id"],
        port=item["port"],
        hostname=item["hostname"],
        enabled=bool(item["enabled"]),
        public_url=item["public_url"],
        last_error=item["last_error"],
        metrics_port=item["metrics_port"],
        guarded=cf.is_guarded(item),
        guard_header_name=item["guard_header_name"],
        # The value itself never leaves the machine — only whether one is set.
        guard_header_set=bool(item["guard_header_value"]),
        guard_secret_path=bool(item["guard_secret"]),
        guard_oauth=bool(item["guard_oauth"]),
        guard_oauth_ready=bool(item["guard_oauth_password"]),
        guard_port=item["guard_port"],
        share_url=cf.share_url(item),
        app_id=runner["id"] if runner else None,
        app_name=runner["name"] if runner else None,
        app_status=status,
        serves_app_id=owner["id"] if owner else None,
        serves_app_name=owner["name"] if owner else None,
        port_open=port_is_listening(item["port"]),
        created_at=item["created_at"],
        updated_at=item["updated_at"],
    )


def _all_routes() -> list[TunnelRoute]:
    serving = _app_index()
    with one_scan():
        return [_to_out(item, serving) for item in cf.routes()]


def _status() -> TunnelStatus:
    installed = cf.binary() is not None
    named = cf.mode() == "named"

    return TunnelStatus(
        installed=installed,
        version=cf.version() if installed else "",
        mode="named" if named else "quick",
        logged_in=cf.logged_in(),
        tunnel_name=cf.tunnel_name(),
        tunnels=[entry["name"] for entry in cf.list_tunnels()],
        config_path=str(cf.CONFIG_PATH) if named else "",
        login_hint="" if named else LOGIN_HINT,
        install_hint="" if installed else INSTALL_HINT,
        routes=_all_routes(),
    )


@router.get("", response_model=TunnelStatus)
def status() -> TunnelStatus:
    # Quick tunnels announce their address only in their own output, so the
    # poll that renders this page is also what discovers it.
    try:
        cf.refresh_urls()
    except Exception:
        pass
    return _status()


@router.post("/routes", response_model=TunnelRoute, status_code=201)
def add_route(payload: TunnelRouteCreate) -> TunnelRoute:
    now = utcnow()
    secret = cf.new_secret() if payload.guard_secret_path else ""
    try:
        with db() as conn:
            cursor = conn.execute(
                """INSERT INTO tunnel_routes
                       (port, hostname, enabled, guard_header_name, guard_header_value,
                        guard_secret, created_at, updated_at)
                   VALUES (?, ?, 0, ?, ?, ?, ?, ?)""",
                (
                    payload.port,
                    payload.hostname,
                    payload.guard_header_name,
                    payload.guard_header_value,
                    secret,
                    now,
                    now,
                ),
            )
            new_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=409,
            detail=f"Port {payload.port} already has a route. Edit that one instead.",
        )

    return _to_out(cf.route(new_id), _app_index())


@router.patch("/routes/{route_id}", response_model=TunnelRoute)
def edit_route(route_id: int, payload: TunnelRouteUpdate) -> TunnelRoute:
    item = cf.route(route_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"route {route_id} not found")

    fields: dict = {}
    if payload.hostname is not None:
        fields["hostname"] = payload.hostname
    if payload.port is not None and payload.port != item["port"]:
        fields["port"] = payload.port

    if payload.guard_header_name is not None:
        fields["guard_header_name"] = payload.guard_header_name
        # A header with no name cannot be sent, so clear the orphaned value
        # rather than keep a credential nothing will ever use.
        if not payload.guard_header_name and payload.guard_header_value is None:
            fields["guard_header_value"] = ""
    if payload.guard_header_value is not None:
        fields["guard_header_value"] = payload.guard_header_value

    if payload.guard_secret_path is not None:
        if payload.guard_secret_path:
            # Regenerating on every save would break a URL already handed out.
            if not item["guard_secret"]:
                fields["guard_secret"] = cf.new_secret()
        else:
            fields["guard_secret"] = ""

    if payload.guard_oauth_password:
        fields["guard_oauth_password"] = oauth.hash_password(payload.guard_oauth_password)

    if payload.guard_oauth is not None:
        fields["guard_oauth"] = 1 if payload.guard_oauth else 0
        if payload.guard_oauth:
            # A consent screen with no password to check would approve anyone
            # who found the URL, which is worse than no OAuth at all.
            has_password = bool(payload.guard_oauth_password) or bool(item["guard_oauth_password"])
            if not has_password:
                raise HTTPException(
                    status_code=400,
                    detail="Set an approval password before switching OAuth on.",
                )
            # OAuth's endpoints have to be findable at the root of the address,
            # and a secret path would hide them from the client that needs them.
            fields["guard_secret"] = ""
        else:
            fields["guard_oauth_password"] = ""

    if not fields:
        return _to_out(item, _app_index())

    # Changing what a live route points at means rebuilding it, so take it
    # down first and put it back up under the new details.
    was_live = bool(item["enabled"])
    if was_live:
        cf.disable(route_id)

    fields["updated_at"] = utcnow()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    try:
        with db() as conn:
            conn.execute(
                f"UPDATE tunnel_routes SET {assignments} WHERE id = ?",
                (*fields.values(), route_id),
            )
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=409,
            detail=f"Port {payload.port} already has a route.",
        )

    # Protection that has just been removed leaves a proxy behind that is now
    # forwarding with nothing to check. cf.disable() clears it for a live
    # route; one that was already off has to be cleaned up here.
    updated = cf.route(route_id)
    if not was_live and updated and not cf.is_guarded(updated) and updated["guard_app_id"]:
        cf.drop_guard(updated)

    if was_live:
        try:
            cf.enable(route_id)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    return _to_out(cf.route(route_id), _app_index())


@router.get("/routes/{route_id}/header-value", response_model=dict)
def reveal_header_value(route_id: int) -> dict:
    """The injected header's value, in the clear, for someone who has lost it.

    Deliberately its own endpoint rather than a field on the route: the tab
    polls that every four seconds, and a credential does not belong in a
    response that is fetched constantly and kept in browser memory. Asking for
    it is an action, so it happens only when someone actually asks.

    Safe to serve because God Father listens on 127.0.0.1 — reaching this
    means already being on the machine, where the database is readable anyway.
    The approval password has no equivalent: it is stored as a one-way hash
    and cannot be produced by anyone, which is the point of it.
    """
    item = cf.route(route_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"route {route_id} not found")
    return {"value": item["guard_header_value"]}


@router.get("/routes/{route_id}/tokens", response_model=list[TunnelToken])
def list_tokens(route_id: int) -> list[TunnelToken]:
    """Keys issued for this route. The keys themselves cannot be listed — only
    their hashes were stored — so this is who holds one, not what it is."""
    if cf.route(route_id) is None:
        raise HTTPException(status_code=404, detail=f"route {route_id} not found")
    return [TunnelToken(**entry) for entry in oauth.list_tokens(route_id)]


@router.delete("/routes/{route_id}/tokens/{token_id}", status_code=204)
def revoke_token(route_id: int, token_id: int) -> None:
    if cf.route(route_id) is None:
        raise HTTPException(status_code=404, detail=f"route {route_id} not found")
    if not oauth.revoke(route_id, token_id):
        raise HTTPException(status_code=404, detail="no such token on this route")


@router.post("/routes/{route_id}/tokens/revoke-all", status_code=204)
def revoke_all_tokens(route_id: int) -> None:
    """Cut every client off at once. They can ask again; nothing is deleted."""
    if cf.route(route_id) is None:
        raise HTTPException(status_code=404, detail=f"route {route_id} not found")
    oauth.revoke_all(route_id)


@router.delete("/routes/{route_id}", status_code=204)
def remove_route(route_id: int) -> None:
    if cf.route(route_id) is None:
        raise HTTPException(status_code=404, detail=f"route {route_id} not found")
    cf.forget(route_id)


@router.post("/routes/{route_id}/up", response_model=TunnelRoute)
def bring_up(route_id: int) -> TunnelRoute:
    try:
        cf.enable(route_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"route {route_id} not found")
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _to_out(cf.route(route_id), _app_index())


@router.post("/routes/{route_id}/down", response_model=TunnelRoute)
def bring_down(route_id: int) -> TunnelRoute:
    try:
        cf.disable(route_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"route {route_id} not found")
    return _to_out(cf.route(route_id), _app_index())


@router.post("/tunnel", response_model=TunnelStatus)
def choose_tunnel(payload: TunnelNameRequest) -> TunnelStatus:
    """Select the named tunnel every route runs on, creating it if asked."""
    if not cf.logged_in():
        raise HTTPException(
            status_code=400,
            detail=(
                "Not logged in yet. Run  cloudflared tunnel login  in a terminal first — "
                "it opens a browser, so it cannot be done from this page."
            ),
        )

    if payload.create and payload.name not in [entry["name"] for entry in cf.list_tunnels()]:
        ok, output = cf.create_tunnel(payload.name)
        if not ok:
            raise HTTPException(status_code=400, detail=f"Could not create the tunnel: {output[:300]}")

    cf.set_tunnel_name(payload.name)
    return _status()


EDGE_NOTE = (
    "Measured on this machine, from each tunnel's own metrics endpoint — so it covers "
    "everything that reached your app. Visitor countries, unique visitors and cache hits "
    "are decided at Cloudflare's edge and never reach here; those need a domain and "
    "Cloudflare's analytics API."
)


@router.get("/metrics", response_model=TunnelMetrics)
def metrics(minutes: int = 60) -> TunnelMetrics:
    """Traffic through every tunnel over the last `minutes`."""
    minutes = max(5, min(minutes, 1440))
    serving = _app_index()

    per_route: list[TunnelRouteMetrics] = []
    for item in cf.routes():
        if not item["enabled"]:
            continue

        window = tm.series(item["id"], minutes)
        recent = tm.latest(item["id"])
        codes = tm.codes_in_window(item["id"], minutes)
        owner = serving.get(item["port"])

        span = max(len(window["points"]), 1)
        rpm = round(sum(p["rpm"] for p in window["points"]) / span, 2) if window["points"] else 0.0
        requests = window["requests"]
        errors = window["errors"]

        per_route.append(
            TunnelRouteMetrics(
                route_id=item["id"],
                port=item["port"],
                public_url=item["public_url"],
                label=owner["name"] if owner else f"port {item['port']}",
                live=True,
                reporting=recent is not None,
                requests=requests,
                errors=errors,
                error_rate=round(errors * 100.0 / requests, 2) if requests else 0.0,
                rpm=rpm,
                peak_rpm=window["peak_rpm"],
                concurrent=recent["concurrent"] if recent else 0,
                connections=recent["connections"] if recent else 0,
                bytes_sent=window["bytes_sent"],
                bytes_received=window["bytes_received"],
                rtt_ms=recent["rtt_ms"] if recent else 0.0,
                edge_location=recent["edge_location"] if recent else "",
                codes=codes,
                points=[TunnelPoint(**point) for point in window["points"]],
            )
        )

    total_requests = sum(route.requests for route in per_route)
    total_errors = sum(route.errors for route in per_route)
    combined: dict[str, int] = {}
    for route in per_route:
        for code, value in route.codes.items():
            combined[code] = combined.get(code, 0) + value

    return TunnelMetrics(
        window_minutes=minutes,
        sampling=tm.sampler.running,
        last_sample=tm.sampler.last_run,
        sample_seconds=tm.SAMPLE_SECONDS,
        retention_hours=tm.RETENTION_HOURS,
        live_routes=len(per_route),
        requests=total_requests,
        errors=total_errors,
        error_rate=round(total_errors * 100.0 / total_requests, 2) if total_requests else 0.0,
        rpm=round(sum(route.rpm for route in per_route), 2),
        concurrent=sum(route.concurrent for route in per_route),
        bytes_sent=sum(route.bytes_sent for route in per_route),
        bytes_received=sum(route.bytes_received for route in per_route),
        codes=dict(sorted(combined.items())),
        routes=per_route,
        note=EDGE_NOTE,
    )


@router.get("/config", response_model=dict)
def show_config() -> dict:
    """The generated config.yml, so what God Father wrote is never a mystery."""
    return {"path": str(cf.CONFIG_PATH), "text": cf.config_text()}
