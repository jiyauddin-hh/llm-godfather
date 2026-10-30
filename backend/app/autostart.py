"""Launch applications flagged to start with God Father itself."""

import json
import threading
import time

from .database import db, record_event
from .focus import stopped_ids as focus_stopped_ids
from .manager import manager
from .network import is_own_relay, reapply_relay, release_relay
from .ports import port_owner

# Give the machine a moment after boot before racing for ports, and space the
# launches out so a dozen apps do not all compile at once.
BOOT_DELAY_SECONDS = 3.0
STAGGER_SECONDS = 1.5

# On a service restart the previous copies are still shutting down, and a port
# can take several seconds to come free. Waiting beats wrongly reporting that
# something else owns it.
PORT_RELEASE_WAIT = 20.0


def _wait_for_port(app_id: int, port: int, timeout: float = PORT_RELEASE_WAIT) -> dict | None:
    """Poll until the port frees up; returns whoever still holds it, or None.

    Our own LAN relay counts as free — we close it so the app can bind, and
    re-open it afterwards once we can tell whether it is still needed.
    """
    deadline = time.monotonic() + timeout
    while True:
        owner = port_owner(port)
        if owner is None:
            return None
        if is_own_relay(owner):
            release_relay(app_id, manager.loop)
            time.sleep(0.3)
            continue
        if time.monotonic() >= deadline:
            return owner
        time.sleep(1.0)


def _launch(row) -> None:
    process = manager.get(row["id"], row["name"])
    if process.is_alive:
        return

    # Something still serving the port after the grace period is genuinely
    # someone else's; starting a second copy would only crash on bind.
    if row["port"]:
        owner = _wait_for_port(row["id"], row["port"])
        if owner:
            who = owner.get("process") or "another process"
            message = f"autostart skipped: port {row['port']} still served by {who}"
            process.emit("system", message)
            record_event(row["id"], "error", message)
            return

    try:
        env = json.loads(row["env"])
    except (json.JSONDecodeError, TypeError):
        env = {}

    try:
        process.start(row["command"], row["working_dir"], env, bool(row["auto_restart"]))
        record_event(row["id"], "started", "autostart with God Father")
    except RuntimeError as exc:
        record_event(row["id"], "error", f"autostart failed: {exc}")
        return

    # Now the app owns its socket, re-open sharing. If it bound 0.0.0.0 this
    # is a no-op, which is exactly right — it is already on the network.
    if row["share_lan"] and row["port"]:
        time.sleep(1.5)
        reapply_relay(row["id"], row["port"], manager.loop)


def run_autostart() -> None:
    """Start flagged apps on a background thread so service startup is not blocked."""

    def worker() -> None:
        time.sleep(BOOT_DELAY_SECONDS)
        try:
            with db() as conn:
                rows = conn.execute(
                    "SELECT * FROM apps WHERE start_on_boot = 1 ORDER BY name COLLATE NOCASE"
                ).fetchall()
        except Exception:
            return

        # Apps Focus Mode switched off stay off until Exit Focus Mode, even
        # across a restart of God Father itself.
        held = focus_stopped_ids()
        for row in rows:
            if row["id"] in held:
                record_event(row["id"], "skipped", "autostart skipped: Focus Mode is on")
        rows = [row for row in rows if row["id"] not in held]

        for index, row in enumerate(rows):
            if index:
                time.sleep(STAGGER_SECONDS)
            try:
                _launch(row)
            except Exception:
                continue

    threading.Thread(target=worker, name="godfather-autostart", daemon=True).start()
