"""Port inspection helpers built on psutil, with graceful degradation."""

import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager

try:
    import psutil
except ImportError:  # psutil is recommended but not fatal
    psutil = None


def is_port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.25) -> bool:
    """True when something accepts connections on the port.

    This opens a real connection, which leaves a TIME-WAIT socket bound to the
    port. An app that does not set SO_REUSEADDR (Python's socketserver.TCPServer,
    among others) then fails to bind with "address already in use". Prefer
    port_is_listening() for routine polling; use this only when reachability
    genuinely has to be proven.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def port_is_listening(port: int) -> bool:
    """Whether anything listens on the port, without opening a connection.

    psutil cannot see sockets owned by other users, so fall back to an actual
    connect only when the passive check finds nothing.
    """
    for entry in listening_ports():
        if entry["port"] == port:
            return True
    return is_port_open(port)


# A port scan walks every process's sockets, which is far too slow to repeat
# once per app while building a list of forty. one_scan() lets a request take
# the scan once and share it; outside such a block every call scans afresh, so
# the start/stop wait loops still see the port the moment it changes.
_scoped = threading.local()


@contextmanager
def one_scan() -> Iterator[None]:
    """Within the block, port lookups on this thread reuse a single scan."""
    if getattr(_scoped, "ports", None) is not None:
        yield
        return
    _scoped.ports = _scan_listening()
    try:
        yield
    finally:
        _scoped.ports = None


def listening_ports() -> list[dict]:
    """Every TCP port in LISTEN state, with the owning process where visible."""
    shared = getattr(_scoped, "ports", None)
    if shared is not None:
        return list(shared)
    return _scan_listening()


def _scan_listening() -> list[dict]:
    if psutil is None:
        return []

    found: dict[int, dict] = {}
    try:
        connections = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError):
        return []

    for conn in connections:
        if conn.status != psutil.CONN_LISTEN or not conn.laddr:
            continue
        port = conn.laddr.port
        if port in found:
            continue

        name = None
        if conn.pid:
            try:
                name = psutil.Process(conn.pid).name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                name = None

        found[port] = {
            "port": port,
            "pid": conn.pid,
            "process": name,
            "address": conn.laddr.ip,
        }

    return sorted(found.values(), key=lambda item: item["port"])


def non_loopback_ports() -> set[int]:
    """Ports with at least one listener on something other than loopback.

    Deliberately not built on listening_ports(), which keeps only one entry per
    port: a port can be bound twice — the app on 127.0.0.1 and our LAN relay on
    the machine's address — and collapsing those loses exactly the fact we need.
    """
    if psutil is None:
        return set()

    reachable: set[int] = set()
    try:
        connections = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError):
        return reachable

    for conn in connections:
        if conn.status != psutil.CONN_LISTEN or not conn.laddr:
            continue
        ip = conn.laddr.ip
        if ip.startswith("127.") or ip == "::1":
            continue
        reachable.add(conn.laddr.port)
    return reachable


def port_owner(port: int) -> dict | None:
    """Details of whoever holds the port, or None when it is free."""
    for entry in listening_ports():
        if entry["port"] == port:
            return entry
    if is_port_open(port):
        return {"port": port, "pid": None, "process": None, "address": "127.0.0.1"}
    return None


def process_stats(pid: int) -> tuple[float | None, float | None]:
    """(cpu_percent, memory_mb) for a pid and its children."""
    if psutil is None:
        return None, None
    try:
        proc = psutil.Process(pid)
        family = [proc, *proc.children(recursive=True)]
        cpu = 0.0
        rss = 0
        for member in family:
            try:
                cpu += member.cpu_percent(interval=None)
                rss += member.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return round(cpu, 1), round(rss / (1024 * 1024), 1)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None, None
