"""Port map: what is listening, and which registered app owns it."""

from fastapi import APIRouter, HTTPException

from ..database import db, record_event
from ..models import KillResult, Occupant, PortInfo
from ..ports import is_port_open, listening_ports, port_owner
from ..takeover import inspect as inspect_occupant
from ..takeover import reclaim

router = APIRouter(prefix="/api/ports", tags=["ports"])


def _registered_ports() -> dict[int, tuple[int, str]]:
    with db() as conn:
        rows = conn.execute("SELECT id, name, port FROM apps WHERE port IS NOT NULL").fetchall()
    return {row["port"]: (row["id"], row["name"]) for row in rows}


@router.get("", response_model=list[PortInfo])
def scan_ports() -> list[PortInfo]:
    known = _registered_ports()
    result: list[PortInfo] = []

    for entry in listening_ports():
        owner = known.get(entry["port"])
        result.append(
            PortInfo(
                **entry,
                managed_app_id=owner[0] if owner else None,
                managed_app_name=owner[1] if owner else None,
            )
        )

    # Registered ports that nothing is listening on still belong on the map.
    seen = {item.port for item in result}
    for port, (app_id, name) in known.items():
        if port not in seen:
            result.append(PortInfo(port=port, managed_app_id=app_id, managed_app_name=name))

    return sorted(result, key=lambda item: item.port)


@router.get("/{port}/occupant", response_model=Occupant)
def port_occupant(port: int) -> Occupant:
    """Who holds this port, and whether God Father is allowed to kill it."""
    return Occupant(**inspect_occupant(port))


@router.post("/{port}/kill", response_model=KillResult)
def kill_port(port: int) -> KillResult:
    """Stop whatever holds this port. Nothing is started in its place."""
    details = inspect_occupant(port)
    if not details.get("occupied"):
        return KillResult(ok=True, message=f"Nothing is listening on port {port}.", port=port)
    if not details.get("killable"):
        raise HTTPException(status_code=409, detail=details.get("reason") or "That process cannot be stopped from here.")

    freed, detail = reclaim(port)
    if not freed:
        raise HTTPException(status_code=409, detail=detail)

    # Attribute the kill to a registered app when the port belongs to one.
    owner = _registered_ports().get(port)
    if owner:
        record_event(owner[0], "killed", detail)

    return KillResult(ok=True, message=detail, port=port)


@router.get("/{port}")
def inspect_port(port: int) -> dict:
    known = _registered_ports()
    owner = port_owner(port)
    registered = known.get(port)

    return {
        "port": port,
        "in_use": owner is not None or is_port_open(port),
        "pid": owner.get("pid") if owner else None,
        "process": owner.get("process") if owner else None,
        "managed_app_id": registered[0] if registered else None,
        "managed_app_name": registered[1] if registered else None,
    }
