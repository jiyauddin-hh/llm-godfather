"""Exposing an application's port to the local network.

An app that binds only to 127.0.0.1 cannot be reached from another machine, and
nothing outside the app itself can change that. So God Father binds the *LAN
address* on the same port number and relays connections to the app's loopback
listener. Binding 192.168.0.167:4200 does not conflict with 127.0.0.1:4200, so
the shared URL keeps the port the user already knows.

An app that already binds 0.0.0.0 needs no relay — only a firewall opening.
"""

import asyncio
import ipaddress
import os
import shutil
import subprocess

try:
    import psutil
except ImportError:
    psutil = None

# Interfaces that are not "the local network" in any useful sense.
SKIP_PREFIXES = ("docker", "br-", "veth", "lo", "virbr", "tun", "tap")


def lan_address() -> tuple[str, str] | None:
    """(ip, subnet_cidr) of this machine's real LAN interface, or None."""
    if psutil is None:
        return None

    for name, addrs in psutil.net_if_addrs().items():
        if name.startswith(SKIP_PREFIXES):
            continue
        for addr in addrs:
            if addr.family != 2 or not addr.address or not addr.netmask:  # AF_INET
                continue
            ip = ipaddress.ip_address(addr.address)
            if ip.is_loopback or ip.is_link_local:
                continue
            network = ipaddress.ip_network(f"{addr.address}/{addr.netmask}", strict=False)
            return addr.address, str(network)
    return None


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (OSError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


class Forwarder:
    """Relays <lan_ip>:port to 127.0.0.1:port for one application."""

    def __init__(self, app_id: int, listen_host: str, port: int):
        self.app_id = app_id
        self.listen_host = listen_host
        self.port = port
        self.server: asyncio.AbstractServer | None = None
        self.connections = 0

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            target_reader, target_writer = await asyncio.open_connection("127.0.0.1", self.port)
        except OSError:
            # App is down; drop the connection rather than hanging the client.
            writer.close()
            return
        await asyncio.gather(
            _pipe(reader, target_writer),
            _pipe(target_reader, writer),
            return_exceptions=True,
        )

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, self.listen_host, self.port)

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception:
                pass
            self.server = None


class ShareRegistry:
    """Live forwarders, keyed by application id."""

    def __init__(self) -> None:
        self._forwarders: dict[int, Forwarder] = {}

    def get(self, app_id: int) -> Forwarder | None:
        return self._forwarders.get(app_id)

    def is_active(self, app_id: int) -> bool:
        forwarder = self._forwarders.get(app_id)
        return forwarder is not None and forwarder.server is not None

    async def enable(self, app_id: int, port: int) -> tuple[bool, str]:
        """Returns (relay_started, human explanation)."""
        if self.is_active(app_id):
            return True, "already shared"

        lan = lan_address()
        if lan is None:
            return False, "no local network interface found"
        ip, _ = lan

        forwarder = Forwarder(app_id, ip, port)
        try:
            await forwarder.start()
        except OSError as exc:
            # EADDRINUSE here means a listener already covers every interface,
            # so the app is reachable on the LAN without any relay from us.
            if exc.errno == 98:
                return False, "app already listens on all interfaces — no relay needed"
            return False, f"could not bind {ip}:{port} — {exc.strerror}"

        self._forwarders[app_id] = forwarder
        return True, f"relaying {ip}:{port} to 127.0.0.1:{port}"

    async def disable(self, app_id: int) -> None:
        forwarder = self._forwarders.pop(app_id, None)
        if forwarder is not None:
            await forwarder.stop()

    async def disable_all(self) -> None:
        for app_id in list(self._forwarders):
            await self.disable(app_id)


shares = ShareRegistry()


def is_own_relay(owner: dict | None) -> bool:
    """True when the process holding a port is this backend's own LAN relay.

    Without this check God Father mistakes its own forwarder for a rival
    process and refuses to start the very app the forwarder exists to expose.
    """
    return bool(owner) and owner.get("pid") == os.getpid()


def release_relay(app_id: int, loop: asyncio.AbstractEventLoop | None, timeout: float = 5.0) -> bool:
    """Close our relay from synchronous code so the app itself can bind the port."""
    if loop is None or not shares.is_active(app_id):
        return False
    try:
        asyncio.run_coroutine_threadsafe(shares.disable(app_id), loop).result(timeout)
        return True
    except Exception:
        return False


def reapply_relay(app_id: int, port: int, loop: asyncio.AbstractEventLoop | None, timeout: float = 6.0) -> None:
    """Re-open the relay after the app is up.

    Run only once the app has bound its own socket: an app listening on
    0.0.0.0 makes this bind fail, which is how we learn no relay is needed.
    """
    if loop is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(shares.enable(app_id, port), loop).result(timeout)
    except Exception:
        pass


# ------------------------------------------------------------------ firewall


def ufw_available() -> bool:
    return shutil.which("ufw") is not None or bool(shutil.which("/usr/sbin/ufw"))


def _ufw(args: list[str], timeout: float = 10.0) -> tuple[bool, str]:
    """Run ufw via passwordless sudo; never prompt, never hang."""
    binary = shutil.which("ufw") or "/usr/sbin/ufw"
    try:
        result = subprocess.run(
            ["sudo", "-n", binary, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)

    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output


def ufw_can_manage() -> bool:
    ok, _ = _ufw(["status"], timeout=5.0)
    return ok


def ufw_rule_args(port: int, subnet: str, delete: bool = False) -> list[str]:
    rule = ["allow", "from", subnet, "to", "any", "port", str(port), "proto", "tcp"]
    return (["delete", *rule]) if delete else rule


def ufw_command_text(port: int, subnet: str, delete: bool = False) -> str:
    return "sudo ufw " + " ".join(ufw_rule_args(port, subnet, delete))


def ufw_open(port: int, subnet: str) -> tuple[bool, str]:
    return _ufw(ufw_rule_args(port, subnet))


def ufw_close(port: int, subnet: str) -> tuple[bool, str]:
    return _ufw(ufw_rule_args(port, subnet, delete=True))


def ufw_is_open(port: int, subnet: str) -> bool:
    ok, output = _ufw(["status"], timeout=5.0)
    if not ok:
        return False
    needle = str(port)
    for line in output.splitlines():
        if needle in line and subnet.split("/")[0].rsplit(".", 1)[0] in line:
            return True
    return False
