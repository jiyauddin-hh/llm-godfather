"""Expose an application's port to the local network, or take it back."""

from fastapi import APIRouter, HTTPException

from ..database import db, record_event, utcnow
from ..manager import manager
from ..models import NetworkInfo, ShareRequest, ShareStatus
from ..network import (
    lan_address,
    shares,
    ufw_available,
    ufw_can_manage,
    ufw_close,
    ufw_command_text,
    ufw_is_open,
    ufw_open,
)
from ..service import fetch_row

router = APIRouter(prefix="/api", tags=["network"])

SUDOERS_HINT = (
    "God Father cannot change firewall rules yet. Install the helper once with:  "
    "sudo ./deploy/install-firewall-rule.sh"
)


@router.get("/network", response_model=NetworkInfo)
def network_info() -> NetworkInfo:
    lan = lan_address()
    installed = ufw_available()
    manageable = ufw_can_manage() if installed else False

    return NetworkInfo(
        lan_ip=lan[0] if lan else None,
        subnet=lan[1] if lan else None,
        ufw_installed=installed,
        ufw_manageable=manageable,
        setup_hint=None if manageable or not installed else SUDOERS_HINT,
    )


@router.post("/apps/{app_id}/share", response_model=ShareStatus)
async def set_share(app_id: int, payload: ShareRequest) -> ShareStatus:
    row = fetch_row(app_id)
    port = row["port"]

    if not port:
        raise HTTPException(
            status_code=400,
            detail="This application has no port set, so there is nothing to share. Add a port first.",
        )

    lan = lan_address()
    if lan is None:
        raise HTTPException(status_code=400, detail="No local network interface was found on this machine.")
    ip, subnet = lan

    firewall_managed = ufw_available() and ufw_can_manage()

    if payload.enabled:
        relaying, detail = await shares.enable(app_id, port)

        firewall_open = None
        if firewall_managed:
            ok, output = ufw_open(port, subnet)
            firewall_open = ok
            if not ok:
                detail = f"{detail}; firewall rule failed: {output[:120]}"

        _persist(app_id, True)
        record_event(app_id, "shared", f"{ip}:{port} on {subnet} ({detail})")

        return ShareStatus(
            enabled=True,
            lan_url=f"http://{ip}:{port}",
            relay_active=relaying,
            detail=detail,
            firewall_open=firewall_open,
            firewall_managed=firewall_managed,
            manual_command=None if firewall_managed else ufw_command_text(port, subnet),
        )

    await shares.disable(app_id)

    firewall_open = None
    if firewall_managed:
        removed, _ = ufw_close(port, subnet)
        firewall_open = not removed

    _persist(app_id, False)
    record_event(app_id, "unshared", f"port {port} closed to {subnet}")

    return ShareStatus(
        enabled=False,
        lan_url=None,
        relay_active=False,
        detail="no longer reachable from the local network",
        firewall_open=firewall_open,
        firewall_managed=firewall_managed,
        manual_command=None if firewall_managed else ufw_command_text(port, subnet, delete=True),
    )


@router.get("/apps/{app_id}/share", response_model=ShareStatus)
def get_share(app_id: int) -> ShareStatus:
    row = fetch_row(app_id)
    port = row["port"]
    lan = lan_address()
    enabled = bool(row["share_lan"])

    if not lan or not port:
        return ShareStatus(enabled=enabled, detail="no port or no local network")

    ip, subnet = lan
    managed = ufw_available() and ufw_can_manage()

    return ShareStatus(
        enabled=enabled,
        lan_url=f"http://{ip}:{port}" if enabled else None,
        relay_active=shares.is_active(app_id),
        detail="relayed by God Father" if shares.is_active(app_id) else "app listens on all interfaces",
        firewall_open=ufw_is_open(port, subnet) if managed else None,
        firewall_managed=managed,
        manual_command=None if managed else ufw_command_text(port, subnet),
    )


def _persist(app_id: int, enabled: bool) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE apps SET share_lan = ?, updated_at = ? WHERE id = ?",
            (int(enabled), utcnow(), app_id),
        )
