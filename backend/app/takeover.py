"""Reclaim a port from a process God Father does not own.

An app started from a terminal keeps its port, so God Father can only refuse to
start its own copy. Taking over means stopping that process first — which is
destructive, so this module's job is to say precisely *what* would be killed,
and to refuse the cases where killing would be wrong or futile.
"""

import os
import signal
import time

try:
    import psutil
except ImportError:
    psutil = None

from .discovery import _roll_up
from .manager import manager
from .network import shares
from .ports import port_is_listening, port_owner


def _relay_uses_port(port: int) -> bool:
    """Whether one of our LAN relays is bound to this port."""
    for forwarder in getattr(shares, "_forwarders", {}).values():
        if forwarder.port == port and forwarder.server is not None:
            return True
    return False

# Killing these breaks far more than the app in front of you.
PROTECTED_NAMES = {
    "systemd", "init", "sshd", "dbus-daemon", "NetworkManager", "gdm", "gnome-shell",
    "Xorg", "wayland", "pipewire", "pulseaudio", "code", "chrome", "firefox",
}

KILL_GRACE_SECONDS = 8.0
PORT_FREE_TIMEOUT = 10.0


def _our_own_pids() -> set[int]:
    """This backend and its ancestors — never a valid kill target."""
    pids = {os.getpid()}
    if psutil is None:
        return pids
    try:
        me = psutil.Process(os.getpid())
        pids |= {parent.pid for parent in me.parents()}
    except Exception:
        pass
    return pids


def _managed_owner(pid: int) -> str | None:
    """Name of the app God Father already manages that owns this pid, if any."""
    if psutil is None:
        return None
    for app_id, process in getattr(manager, "_processes", {}).items():
        proc = process.proc
        if proc is None or proc.poll() is not None:
            continue
        if proc.pid == pid:
            return process.name
        try:
            if any(child.pid == pid for child in psutil.Process(proc.pid).children(recursive=True)):
                return process.name
        except Exception:
            continue
    return None


def inspect(port: int) -> dict:
    """Describe whoever holds the port and whether we may kill it."""
    owner = port_owner(port)
    if owner is None:
        return {"occupied": False}

    pid = owner.get("pid")
    result = {
        "occupied": True,
        "pid": pid,
        "process": owner.get("process"),
        "command": None,
        "user": None,
        "killable": False,
        "reason": None,
        "managed_by": None,
    }

    if pid is None:
        result["reason"] = (
            "The owning process belongs to another user, so it cannot be identified or stopped "
            "without root. Stop it the way you started it."
        )
        return result

    if pid in _our_own_pids():
        # Our own socket on an app's port is the LAN relay, not a rival app.
        if _relay_uses_port(port):
            result["reason"] = (
                f"Port {port} is held by God Father's own network relay for a shared app. "
                f"Turn off local network sharing for that app instead."
            )
        else:
            result["reason"] = "That is God Father itself."
        return result

    managed = _managed_owner(pid)
    if managed:
        result["managed_by"] = managed
        result["reason"] = f"Port held by '{managed}', which God Father already manages. Stop that app instead."
        return result

    if psutil is None:
        result["reason"] = "psutil is not installed, so the process cannot be inspected."
        return result

    try:
        proc = psutil.Process(pid)
        launcher = _roll_up(proc)
        result["pid"] = launcher.pid
        result["process"] = launcher.name()
        result["command"] = " ".join(launcher.cmdline())[:300] or launcher.name()
        result["user"] = launcher.username()
    except Exception as exc:
        result["reason"] = f"Could not inspect the process: {exc}"
        return result

    if result["process"] in PROTECTED_NAMES:
        result["reason"] = f"'{result['process']}' is a system process and will not be killed."
        return result

    try:
        if result["user"] != psutil.Process(os.getpid()).username():
            result["reason"] = (
                f"Runs as '{result['user']}', not you — stopping it needs root. "
                f"It is probably a system service; use systemctl."
            )
            return result
    except Exception:
        pass

    result["killable"] = True
    return result


def reclaim(port: int) -> tuple[bool, str]:
    """Stop the process holding the port and wait for the port to come free."""
    details = inspect(port)
    if not details["occupied"]:
        return True, f"port {port} was already free"
    if not details["killable"]:
        return False, details["reason"] or "that process cannot be stopped from here"

    pid = details["pid"]
    name = details["process"]

    # The port holder is usually a child of the command a human ran, so signal
    # the whole group — killing only the listener leaves the parent behind.
    try:
        target_group = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        target_group = None

    try:
        if target_group is not None and target_group != os.getpgid(os.getpid()):
            os.killpg(target_group, signal.SIGTERM)
        else:
            psutil.Process(pid).terminate()
    except (ProcessLookupError, psutil.NoSuchProcess):
        return True, f"{name} had already exited"
    except (PermissionError, OSError) as exc:
        return False, f"could not signal {name} (pid {pid}): {exc}"

    deadline = time.monotonic() + KILL_GRACE_SECONDS
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            break
        time.sleep(0.25)
    else:
        try:
            if target_group is not None:
                os.killpg(target_group, signal.SIGKILL)
            else:
                psutil.Process(pid).kill()
        except Exception:
            pass

    freed_at = time.monotonic() + PORT_FREE_TIMEOUT
    while time.monotonic() < freed_at:
        if port_owner(port) is None and not port_is_listening(port):
            return True, f"stopped {name} (pid {pid}) and freed port {port}"
        time.sleep(0.4)

    return False, f"{name} was signalled but port {port} is still held"
