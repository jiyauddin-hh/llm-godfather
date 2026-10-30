"""Publish a local port to the internet through Cloudflare Tunnel.

Two modes, chosen by whether `cloudflared tunnel login` has been run on this
machine:

* **quick** — needs no domain and no account. Every live route gets its own
  `cloudflared tunnel --url` process and a random `*.trycloudflare.com`
  address, which we read back out of the process's own output because quick
  tunnels announce their address nowhere else.
* **named** — a domain is on Cloudflare. One process serves every route from a
  generated `config.yml`, each on a hostname you choose, and the addresses are
  stable across restarts.

Routes live in SQLite and are the source of truth; `config.yml` is generated
from them. That is what makes the two modes one feature rather than two: the
day a domain arrives, the routes already exist and only the plumbing under
them changes.

Each live route is backed by a normal God Father application row, so a tunnel
starts, stops, restarts, logs and auto-restarts exactly like everything else
on the Applications screen.
"""

import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .config import BASE_DIR
from .database import db, record_event, utcnow
from .manager import manager
from .ports import port_is_listening

HOME = Path.home()
CONFIG_DIR = HOME / ".cloudflared"
CERT_PATH = CONFIG_DIR / "cert.pem"
CONFIG_PATH = CONFIG_DIR / "config.yml"

# cloudflared's local metrics endpoint. Pinned explicitly per tunnel: left to
# itself cloudflared picks any free port in a range, and God Father would then
# have nothing stable to health-check.
METRICS_BASE = 20241
METRICS_LIMIT = 20340

# Guard proxies get their own band, immediately above the metrics one, so the
# two allocators can never hand out the same number.
GUARD_BASE = 20341
GUARD_LIMIT = 20440

# Length of a generated secret path prefix, in bytes before hex encoding.
SECRET_BYTES = 24

# Quick tunnels print their address once, inside a banner on stderr.
QUICK_URL = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")

# The single row that serves every route once a domain is configured.
NAMED_ROW_NAME = "cloudflare-tunnel"

SETTING_TUNNEL = "cloudflare_tunnel_name"

CLI_TIMEOUT = 25.0


# ------------------------------------------------------------------ the binary


def binary() -> str | None:
    """Locate cloudflared, including the per-user install path PATH may miss."""
    found = shutil.which("cloudflared")
    if found:
        return found
    candidate = HOME / ".local" / "bin" / "cloudflared"
    return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None


def _command_exe() -> str:
    """The cloudflared path to write into a tunnel's command line.

    Tunnels run as ordinary applications, which means /bin/sh with the
    server's own environment. When God Father is a systemd service that PATH
    is the system default and does not include ~/.local/bin, where cloudflared
    installs itself for a single user. A bare `cloudflared` there exits 127
    before it can print anything, so the route goes live with no address and
    no error to explain it. Resolve the absolute path here instead.
    """
    return shlex.quote(binary() or "cloudflared")


def _run(args: list[str], timeout: float = CLI_TIMEOUT) -> tuple[bool, str]:
    """Call the cloudflared CLI, returning (ok, combined output)."""
    exe = binary()
    if not exe:
        return False, "cloudflared is not installed"
    try:
        result = subprocess.run(
            [exe, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, f"cloudflared {' '.join(args)} timed out"
    except OSError as exc:
        return False, str(exc)

    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, output.strip()


def version() -> str:
    ok, output = _run(["--version"], timeout=8.0)
    return output.splitlines()[0].strip() if ok and output else ""


def logged_in() -> bool:
    """Whether `cloudflared tunnel login` has left a certificate here.

    This is the only thing that separates the two modes, so it is read fresh
    every time rather than cached — logging in should take effect on the next
    page refresh, not the next restart.
    """
    return CERT_PATH.is_file()


def mode() -> str:
    return "named" if logged_in() else "quick"


def list_tunnels() -> list[dict]:
    """Named tunnels on the logged-in account. Empty in quick mode."""
    if not logged_in():
        return []
    ok, output = _run(["tunnel", "list", "--output", "json"])
    if not ok:
        return []
    try:
        import json

        parsed = json.loads(output)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [
        {"id": item.get("id", ""), "name": item.get("name", "")}
        for item in parsed
        if isinstance(item, dict) and item.get("name")
    ]


def create_tunnel(name: str) -> tuple[bool, str]:
    return _run(["tunnel", "create", name])


def route_dns(tunnel: str, hostname: str) -> tuple[bool, str]:
    """Create the proxied CNAME for a hostname. Never deletes one."""
    return _run(["tunnel", "route", "dns", tunnel, hostname])


# ------------------------------------------------------------------- routes


def routes() -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM tunnel_routes ORDER BY port").fetchall()
    return [dict(row) for row in rows]


def route(route_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM tunnel_routes WHERE id = ?", (route_id,)).fetchone()
    return dict(row) if row else None


def _set(route_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = utcnow()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with db() as conn:
        conn.execute(
            f"UPDATE tunnel_routes SET {assignments} WHERE id = ?",
            (*fields.values(), route_id),
        )


def _free_metrics_port(exclude_route: int | None = None) -> int:
    """Pick a metrics port no other route has claimed and nothing is serving."""
    with db() as conn:
        rows = conn.execute(
            "SELECT id, metrics_port FROM tunnel_routes WHERE metrics_port IS NOT NULL"
        ).fetchall()
    taken = {row["metrics_port"] for row in rows if row["id"] != exclude_route}

    for candidate in range(METRICS_BASE, METRICS_LIMIT):
        if candidate not in taken and not port_is_listening(candidate):
            return candidate
    raise RuntimeError("no free local port left for cloudflared's metrics endpoint")


# ------------------------------------------------------------------- guards


def is_guarded(item: dict) -> bool:
    """Whether this route needs a proxy in front of it."""
    return bool(item["guard_header_name"] or item["guard_secret"] or item["guard_oauth"])


def new_secret() -> str:
    return secrets.token_hex(SECRET_BYTES)


def target_port(item: dict) -> int:
    """The port the tunnel should actually serve.

    A guarded route is published through its proxy, never straight at the
    application — otherwise the guard could be walked around by anyone who
    learned the real port.
    """
    if is_guarded(item) and item["guard_port"]:
        return item["guard_port"]
    return item["port"]


def share_url(item: dict) -> str:
    """The address to hand out, including the secret path when there is one."""
    if not item["public_url"]:
        return ""
    if not item["guard_secret"]:
        return item["public_url"]
    return f"{item['public_url'].rstrip('/')}/{item['guard_secret']}"


def _free_guard_port(exclude_route: int | None = None) -> int:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, guard_port FROM tunnel_routes WHERE guard_port IS NOT NULL"
        ).fetchall()
    taken = {row["guard_port"] for row in rows if row["id"] != exclude_route}

    for candidate in range(GUARD_BASE, GUARD_LIMIT):
        if candidate not in taken and not port_is_listening(candidate):
            return candidate
    raise RuntimeError("no free local port left for a guard proxy")


def _ensure_guard(item: dict) -> int:
    """Start (or restart) the guard in front of one route, returning its port.

    The route id is the only argument on the command line — the guard reads
    the credential out of the database itself, so it never reaches `ps`.
    """
    guard_port = item["guard_port"] or _free_guard_port(exclude_route=item["id"])
    command = f"{shlex.quote(sys.executable)} -m app.guard {item['id']}"

    app_id = _ensure_app_row(
        name=f"guard-{item['port']}",
        description=f"Guard proxy protecting port {item['port']}",
        command=command,
        port=guard_port,
        app_id=item["guard_app_id"],
        working_dir=str(BASE_DIR),
    )

    # Written before the process starts: the guard looks guard_port up itself.
    _set(item["id"], guard_port=guard_port, guard_app_id=app_id)
    _start_row(app_id)
    record_event(app_id, "started", f"guard proxy for port {item['port']}")
    return guard_port


def drop_guard(item: dict) -> None:
    """Stop and forget a route's guard, leaving its settings on the route."""
    if item["guard_app_id"]:
        _drop_app_row(item["guard_app_id"])
    _set(item["id"], guard_app_id=None)


# --------------------------------------------------------------- app rows

def _app_row(app_id: int | None) -> dict | None:
    if not app_id:
        return None
    with db() as conn:
        row = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
    return dict(row) if row else None


def _unique_name(base: str) -> str:
    with db() as conn:
        taken = {row["name"] for row in conn.execute("SELECT name FROM apps")}
    if base not in taken:
        return base
    suffix = 2
    while f"{base}-{suffix}" in taken:
        suffix += 1
    return f"{base}-{suffix}"


def _ensure_app_row(
    name: str,
    description: str,
    command: str,
    port: int,
    app_id: int | None,
    working_dir: str | None = None,
) -> int:
    """Create or update the application row that runs one tunnel.

    working_dir defaults to the home directory, which is all a tunnel needs.
    A guard is started as `python -m app.guard`, so it has to run from the
    backend package's own directory instead.
    """
    now = utcnow()
    existing = _app_row(app_id)
    cwd = working_dir or str(HOME)

    if existing:
        with db() as conn:
            conn.execute(
                """UPDATE apps SET description = ?, command = ?, working_dir = ?, port = ?,
                                   auto_restart = 1, start_on_boot = 1, updated_at = ?
                   WHERE id = ?""",
                (description, command, cwd, port, now, existing["id"]),
            )
        return existing["id"]

    with db() as conn:
        cursor = conn.execute(
            """INSERT INTO apps (name, description, command, working_dir, port, env,
                                 auto_restart, start_on_boot, share_lan, live_reload,
                                 preview_url, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, '{}', 1, 1, 0, 0, '', ?, ?)""",
            (_unique_name(name), description, command, cwd, port, now, now),
        )
        new_id = cursor.lastrowid

    record_event(new_id, "created", "created by the Cloudflare tab")
    return new_id


def _drop_app_row(app_id: int | None) -> None:
    """Stop and remove a row we created. Rows we did not create are left alone."""
    if not app_id:
        return
    process = manager.peek(app_id)
    if process is not None and process.is_alive:
        process.stop()
    manager.forget(app_id)
    with db() as conn:
        conn.execute("DELETE FROM apps WHERE id = ?", (app_id,))


def _start_row(app_id: int) -> None:
    row = _app_row(app_id)
    if not row:
        return
    process = manager.get(app_id, row["name"])
    if process.is_alive:
        process.stop()
    process.start(row["command"], row["working_dir"], {}, True)


# ------------------------------------------------------------ named mode config


def _config_text(name: str, entries: list[dict]) -> str:
    """Render config.yml. Order matters: the catch-all must come last."""
    lines = [
        "# Generated by God Father — edits here are overwritten.",
        "# Manage these routes from the Cloudflare tab.",
        f"tunnel: {name}",
    ]

    credentials = _credentials_path(name)
    if credentials:
        lines.append(f"credentials-file: {credentials}")

    lines.append("")
    lines.append("ingress:")
    for entry in entries:
        lines.append(f"  - hostname: {entry['hostname']}")
        # A guarded route is served through its proxy, so that is what the
        # ingress rule names — never the application's own port.
        lines.append(f"    service: http://localhost:{target_port(entry)}")
    lines.append("  - service: http_status:404")
    lines.append("")
    return "\n".join(lines)


def _credentials_path(name: str) -> str:
    """The per-tunnel credentials file `tunnel create` wrote, if we can find it."""
    for entry in list_tunnels():
        if entry["name"] == name and entry["id"]:
            candidate = CONFIG_DIR / f"{entry['id']}.json"
            if candidate.is_file():
                return str(candidate)
    return ""


def write_config(name: str, entries: list[dict]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(_config_text(name, entries), encoding="utf-8")


def config_text() -> str:
    try:
        return CONFIG_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


# --------------------------------------------------------------- public URLs


def _read_quick_url(app_id: int | None) -> str:
    """Fish the trycloudflare address out of a running quick tunnel's output.

    Quick tunnels publish their address nowhere but their own log, so this is
    the only way to learn it. Reading the tail on demand — rather than watching
    the stream in a thread — means a tunnel that was restarted by autostart,
    auto-restart or a reboot re-reports itself on the next poll with no extra
    machinery.
    """
    if not app_id:
        return ""
    process = manager.peek(app_id)
    if process is None:
        return ""
    # Newest first: after an auto-restart the log holds every address the
    # tunnel has ever had, and only the last one still resolves.
    for line in reversed(process.tail(400)):
        match = QUICK_URL.search(line.get("text", ""))
        if match:
            return match.group(0)
    return ""


# A tunnel takes a moment between launching and printing its address, so only
# a process that has already finished counts as an explanation for a blank one.
DEAD_STATUSES = ("crashed", "stopped")

# Log lines God Father writes about the process rather than lines the process
# wrote itself. Skipped when hunting for the reason a tunnel died.
OWN_LOG_PREFIXES = ("started ", "auto-restarting", "process exited", "auto-restart disabled")


def _failure_note(item: dict) -> str:
    """Why a live route has no working address, read off the process behind it.

    Enabling a route succeeds even when the tunnel it starts dies a second
    later, and a dead tunnel reports nothing at all — no address, and no error,
    because nothing about the route itself went wrong. Without this the tab
    shows a live route waiting forever on an address that is never coming.
    """
    process = manager.peek(item["app_id"]) if item["app_id"] else None
    if process is None:
        return "The tunnel is not running. Switch this route off and on to start it again."
    if process.status not in DEAD_STATUSES:
        return ""

    detail = ""
    for line in reversed(process.tail(60)):
        text = line.get("text", "").strip()
        if text and not text.startswith(OWN_LOG_PREFIXES):
            detail = text
            break

    code = process.exit_code
    reason = f"exit code {code}" if code is not None else "it stopped"
    return f"The tunnel is not running ({reason}). {detail}".strip()


def refresh_urls() -> None:
    """Bring every route's stored address back in line with what is running."""
    named = mode() == "named"
    for item in routes():
        if not item["enabled"]:
            if item["public_url"] or item["last_error"]:
                _set(item["id"], public_url="", last_error="")
            continue

        if named:
            wanted = f"https://{item['hostname']}" if item["hostname"] else ""
        else:
            wanted = _read_quick_url(item["app_id"])

        # An address we already have beats a blank read: the log tail is
        # trimmed eventually, and losing the URL from under someone mid-session
        # would look like a failure that has not happened.
        if wanted and wanted != item["public_url"]:
            _set(item["id"], public_url=wanted)

        # Reported even when an address is on file: in named mode the hostname
        # is known up front, and a crashed tunnel is exactly why it 502s.
        note = _failure_note(item)
        if note != item["last_error"]:
            _set(item["id"], last_error=note)


# ------------------------------------------------------------------ lifecycle


def enable(route_id: int) -> dict:
    """Bring one route up, in whichever mode this machine is in."""
    item = route(route_id)
    if item is None:
        raise ValueError(f"route {route_id} not found")

    exe = binary()
    if not exe:
        raise RuntimeError(
            "cloudflared is not installed. Install it, then reload this tab."
        )

    if mode() == "named":
        return _enable_named(item)
    return _enable_quick(item)


def _enable_quick(item: dict) -> dict:
    metrics = item["metrics_port"] or _free_metrics_port(exclude_route=item["id"])

    # The guard has to be listening before cloudflared reaches for it, or the
    # first requests through the new address get a 502.
    if is_guarded(item):
        _ensure_guard(item)
        item = route(item["id"])

    command = (
        f"{_command_exe()} tunnel --no-autoupdate "
        f"--metrics localhost:{metrics} "
        f"--url http://localhost:{target_port(item)}"
    )
    app_id = _ensure_app_row(
        name=f"cf-{item['port']}",
        description=f"Cloudflare quick tunnel for port {item['port']}",
        command=command,
        port=metrics,
        app_id=item["app_id"],
    )

    _set(item["id"], enabled=1, app_id=app_id, metrics_port=metrics, last_error="", public_url="")

    try:
        _start_row(app_id)
    except RuntimeError as exc:
        _set(item["id"], last_error=str(exc))
        raise

    record_event(app_id, "shared", f"quick tunnel opened for port {item['port']}")
    return route(item["id"])


def _enable_named(item: dict) -> dict:
    if not item["hostname"]:
        raise RuntimeError(
            "This route needs a hostname before it can go live. Add one — for "
            "example app.example.com — on a domain in your Cloudflare account."
        )

    name = tunnel_name()
    if not name:
        raise RuntimeError(
            "No tunnel is selected yet. Choose or create one at the top of this tab."
        )

    ok, output = route_dns(name, item["hostname"])
    if not ok and "already exists" not in output.lower():
        _set(item["id"], last_error=output[:400])
        raise RuntimeError(f"Cloudflare refused the DNS record: {output[:300]}")

    if is_guarded(item):
        _ensure_guard(item)

    _set(item["id"], enabled=1, last_error="", public_url=f"https://{item['hostname']}")
    _apply_named()
    return route(item["id"])


def _apply_named() -> dict | None:
    """Regenerate config.yml from every live route and restart the one process."""
    name = tunnel_name()
    if not name:
        return None

    entries = [
        item for item in routes()
        if item["enabled"] and item["hostname"]
    ]

    # Every guard has to be up before the config naming its port is written,
    # otherwise the one process serving all routes starts 502ing the guarded
    # ones. Re-read afterwards so the ingress rules see the assigned ports.
    if any(is_guarded(entry) for entry in entries):
        for entry in entries:
            if is_guarded(entry):
                _ensure_guard(entry)
        by_id = {item["id"]: item for item in routes()}
        entries = [by_id[entry["id"]] for entry in entries if entry["id"] in by_id]

    write_config(name, entries)

    if not entries:
        # Nothing left to serve — take the process down rather than leave it
        # running a config whose only rule is a 404.
        row = _named_row()
        if row:
            _drop_app_row(row["id"])
        return None

    metrics = METRICS_BASE
    command = (
        f"{_command_exe()} tunnel --no-autoupdate "
        f"--metrics localhost:{metrics} run {name}"
    )
    existing = _named_row()
    app_id = _ensure_app_row(
        name=NAMED_ROW_NAME,
        description=f"Cloudflare tunnel '{name}' — {len(entries)} route(s)",
        command=command,
        port=metrics,
        app_id=existing["id"] if existing else None,
    )

    # Every route shares this row, so they all point at it.
    with db() as conn:
        conn.execute(
            "UPDATE tunnel_routes SET app_id = ?, metrics_port = ? WHERE enabled = 1",
            (app_id, metrics),
        )

    _start_row(app_id)
    return _app_row(app_id)


def _named_row() -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM apps WHERE name = ? OR name LIKE ?",
            (NAMED_ROW_NAME, f"{NAMED_ROW_NAME}-%"),
        ).fetchone()
    return dict(row) if row else None


def disable(route_id: int) -> dict:
    """Take one route down, leaving the route itself intact for later."""
    item = route(route_id)
    if item is None:
        raise ValueError(f"route {route_id} not found")

    # The guard exists to serve this route. Nothing should be left listening
    # on a local port once the route it fronts is off the internet.
    drop_guard(item)

    if mode() == "named":
        _set(item["id"], enabled=0, public_url="")
        _apply_named()
        return route(item["id"])

    _drop_app_row(item["app_id"])
    _set(item["id"], enabled=0, app_id=None, public_url="")
    return route(item["id"])


def forget(route_id: int) -> None:
    """Delete a route entirely, stopping it first if it is live."""
    item = route(route_id)
    if item is None:
        return
    if item["enabled"]:
        try:
            disable(route_id)
        except Exception:
            pass
    # Belt and braces: disable() already drops it, but a route switched off
    # by an earlier version could still have one on file.
    drop_guard(route(route_id) or item)
    with db() as conn:
        conn.execute("DELETE FROM tunnel_routes WHERE id = ?", (route_id,))
    if mode() == "named":
        _apply_named()


def reconcile() -> None:
    """Called at startup, after autostart has had its turn.

    Autostart already relaunches every tunnel row, because they are ordinary
    applications with start-on-boot set. What is left is the bookkeeping: drop
    routes whose row was deleted from the Applications screen, and clear
    addresses that belong to a previous run.
    """
    for item in routes():
        if not item["enabled"]:
            continue
        if item["app_id"] and _app_row(item["app_id"]) is None:
            # Someone deleted the row by hand. Honour that.
            _set(item["id"], enabled=0, app_id=None, public_url="", last_error="")
            continue
        # A guard's row is restarted by autostart like any other. Only a row
        # deleted by hand needs anything here: forget it, so that switching
        # the route off and on builds a fresh one instead of chasing a ghost.
        if is_guarded(item) and item["guard_app_id"] and _app_row(item["guard_app_id"]) is None:
            _set(item["id"], guard_app_id=None)
    if mode() == "named":
        try:
            _apply_named()
        except Exception:
            pass


# ------------------------------------------------------------------ settings


def tunnel_name() -> str:
    from .database import get_setting

    return get_setting(SETTING_TUNNEL, "")


def set_tunnel_name(name: str) -> None:
    from .database import set_setting

    set_setting(SETTING_TUNNEL, name)
