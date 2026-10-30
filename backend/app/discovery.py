"""Discover applications already running on this machine.

Two passes are needed because a normal user cannot see which process owns a
socket belonging to another user:

1. **Port pass** — for sockets we can attribute (our own processes), roll the
   process up to the command a human would actually type, and group ports that
   belong to the same launcher.
2. **Name pass** — well-known services (MySQL, MongoDB, ollama …) run as their
   own user, so their port is anonymous to us. We find them by process name and
   pair them with their default port, confirmed by an actual connect.
"""

import os
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None

from .config import BASE_DIR
from .ports import is_port_open, listening_ports

PROJECT_ROOT = BASE_DIR.parent

# Processes that are never interesting as "applications".
IGNORE_NAMES = {
    "code", "code-oss", "chrome", "firefox", "brave", "electron",
    "systemd", "systemd-resolve", "systemd-resolved", "cupsd", "smbd", "nmbd",
    "sshd", "avahi-daemon", "dnsmasq", "cups-browsed", "containerd", "rpcbind",
    "postfix", "master", "exim4", "snapd", "gvfsd", "fwupd",
}

IGNORE_CMDLINE_MARKERS = (
    "--type=utility", "--type=renderer", "--type=gpu-process",
    "vscode-server", "extensionHost", "/usr/share/code",
    "shell-snapshots",
)

# name -> (label, default port)
KNOWN_SERVICES = {
    "mysqld": ("MySQL", 3306),
    "mariadbd": ("MariaDB", 3306),
    "mongod": ("MongoDB", 27017),
    "postgres": ("PostgreSQL", 5432),
    "redis-server": ("Redis", 6379),
    "memcached": ("Memcached", 11211),
    "ollama": ("Ollama", 11434),
    "elasticsearch": ("Elasticsearch", 9200),
}

TOOL_NAMES = {"ngrok", "cloudflared", "localtunnel", "ollama"}

# Ancestors worth rolling up to: the command a human typed.
RUNNER_NAMES = {"npm", "yarn", "pnpm", "bun", "make", "deno"}

# Walking up stops unconditionally at these.
HARD_BOUNDARY = {"systemd", "init", "code", "sshd", "tmux", "login", "containerd", "dockerd"}


def _safe(getter, default=None):
    try:
        return getter()
    except Exception:
        return default


def _is_shell_wrapper(proc) -> bool:
    """`sh -c …` / `bash -c …`, the wrapper npm puts around every script."""
    name = _safe(proc.name, "") or ""
    if name not in {"sh", "bash", "dash", "zsh"}:
        return False
    cmdline = _safe(proc.cmdline, []) or []
    return "-c" in cmdline


def _first_token(proc) -> str:
    cmdline = _safe(proc.cmdline, []) or []
    if not cmdline:
        return ""
    return Path(cmdline[0]).name


def _roll_up(proc):
    """Find the ancestor representing the command a human would re-type.

    `vite` started by `npm run dev` under `concurrently` under `npm start`
    rolls all the way up to `npm start`. A uvicorn reload worker rolls up to
    its reloader parent. Anything else stays as itself.
    """
    own_cwd = _safe(proc.cwd)
    chain = []
    current = proc

    for _ in range(12):
        parent = _safe(current.parent)
        if parent is None or parent.pid <= 1:
            break
        name = _safe(parent.name, "") or ""
        if name in HARD_BOUNDARY:
            break
        # An interactive shell is where the human sits — stop there.
        if name in {"sh", "bash", "dash", "zsh", "fish"} and not _is_shell_wrapper(parent):
            break
        chain.append(parent)
        current = parent

    same_cwd = [p for p in chain if own_cwd and _safe(p.cwd) == own_cwd]

    # Prefer the highest package-manager ancestor (`npm start` over `vite`).
    runners = [
        p for p in same_cwd
        if (_safe(p.name, "") or "") in RUNNER_NAMES or _first_token(p) in RUNNER_NAMES
    ]
    if runners:
        return runners[-1]

    # Otherwise a same-executable parent, which catches reloaders and masters
    # (uvicorn --reload, gunicorn) whose child cmdline is an internal artefact.
    own_exe = _safe(proc.exe)
    if own_exe:
        twins = [p for p in same_cwd if _safe(p.exe) == own_exe]
        if twins:
            return twins[-1]

    return proc


def _looks_ignorable(proc) -> bool:
    name = _safe(proc.name, "") or ""
    if name in IGNORE_NAMES:
        return True
    cmdline = " ".join(_safe(proc.cmdline, []) or [])
    return any(marker in cmdline for marker in IGNORE_CMDLINE_MARKERS)


def _is_our_own(proc) -> bool:
    """God Father's own backend and frontend must not offer to manage themselves."""
    cwd = _safe(proc.cwd)
    if cwd:
        try:
            Path(cwd).resolve().relative_to(PROJECT_ROOT.resolve())
            return True
        except ValueError:
            pass
    cmdline = " ".join(_safe(proc.cmdline, []) or [])
    return "app.main:app" in cmdline and str(PROJECT_ROOT) in cmdline


# Folder names that say nothing about which project this is.
GENERIC_DIRS = {"backend", "frontend", "server", "client", "api", "app", "src", "web", "dist"}


def _project_name(cwd: str) -> str:
    """`…/odin version 0.2/backend` should read as odin, not as backend."""
    path = Path(cwd)
    if path.name.lower() in GENERIC_DIRS and path.parent.name:
        return f"{path.parent.name}-{path.name}"
    return path.name


def _slug(text: str) -> str:
    cleaned = "".join(char if char.isalnum() else "-" for char in text.strip().lower())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "app"


def _command_of(proc) -> str:
    cmdline = [part for part in (_safe(proc.cmdline, []) or []) if part.strip()]
    if not cmdline:
        return _safe(proc.name, "") or ""

    # Tools like npm rewrite argv into a single padded title ("npm start    "),
    # which is already a shell command — quoting it would make it unrunnable.
    if len(cmdline) == 1:
        return cmdline[0].strip()

    return " ".join(f'"{part}"' if " " in part else part for part in cmdline)


def _port_pass(ignore_pids: set[int]) -> dict[int, dict]:
    """Group attributable listening ports by the launcher they roll up to."""
    grouped: dict[int, dict] = {}

    for entry in listening_ports():
        pid = entry.get("pid")
        if not pid or pid in ignore_pids:
            continue
        try:
            proc = psutil.Process(pid)
        except Exception:
            continue
        if _looks_ignorable(proc) or _is_our_own(proc):
            continue

        root = _roll_up(proc)
        if _looks_ignorable(root) or _is_our_own(root):
            continue

        cwd = _safe(root.cwd)
        if not cwd or not Path(cwd).is_dir():
            continue

        bucket = grouped.setdefault(
            root.pid,
            {"proc": root, "cwd": cwd, "ports": set(), "name": _safe(root.name, "") or ""},
        )
        bucket["ports"].add(entry["port"])

    return grouped


def _name_pass(seen_ports: set[int]) -> list[dict]:
    """Find well-known services by process name, since their sockets are opaque."""
    found: list[dict] = []
    claimed: set[str] = set()

    for proc in psutil.process_iter(["name"]):
        raw_name = (proc.info.get("name") or "").lower()
        if raw_name not in KNOWN_SERVICES or raw_name in claimed:
            continue

        label, default_port = KNOWN_SERVICES[raw_name]
        if default_port in seen_ports or not is_port_open(default_port):
            continue

        claimed.add(raw_name)
        found.append(
            {
                "proc": proc,
                "cwd": _safe(proc.cwd) or "/",
                "ports": {default_port},
                "name": raw_name,
                "label": label,
            }
        )

    return found


def _classify(name: str, cwd: str) -> str:
    if name.lower() in KNOWN_SERVICES and name.lower() not in TOOL_NAMES:
        return "service"
    if name.lower() in TOOL_NAMES:
        return "tool"
    home = str(Path.home())
    if cwd and cwd.startswith(home) and cwd != home:
        return "project"
    return "service"


def discover(registered: list[dict]) -> list[dict]:
    """Return importable candidates, annotated against what is already registered."""
    if psutil is None:
        return []

    ignore_pids = {os.getpid()}
    try:
        ignore_pids |= {child.pid for child in psutil.Process(os.getpid()).children(recursive=True)}
    except Exception:
        pass

    grouped = _port_pass(ignore_pids)
    seen_ports = {port for bucket in grouped.values() for port in bucket["ports"]}
    buckets = list(grouped.values()) + _name_pass(seen_ports)

    by_port = {app["port"]: app for app in registered if app.get("port")}
    by_location = {(app["working_dir"], app["command"]): app for app in registered}

    candidates: list[dict] = []
    for bucket in buckets:
        proc = bucket["proc"]
        cwd = bucket["cwd"]
        ports = sorted(bucket["ports"])
        primary = ports[0] if ports else None
        raw_name = bucket["name"]
        kind = _classify(raw_name, cwd)

        label = bucket.get("label")
        if label:
            suggested = _slug(label)
        elif kind == "project":
            suggested = _slug(_project_name(cwd))
        else:
            suggested = _slug(raw_name)

        command = _command_of(proc)
        note = None
        importable = True

        if kind == "service":
            note = (
                "Runs as a system service. Starting or stopping it from here will likely "
                "fail or fight with systemd — register it mainly so its port is on the map."
            )
        elif len(ports) > 1:
            note = f"One launcher serving ports {', '.join(str(p) for p in ports)}."

        if not Path(cwd).is_dir():
            importable = False
            note = "Working directory is not readable, so this cannot be registered."

        existing = by_port.get(primary) or by_location.get((cwd, command))

        candidates.append(
            {
                "suggested_name": suggested,
                "description": f"{label} service" if label else f"Discovered on port {primary}",
                "command": command,
                "working_dir": cwd,
                "port": primary,
                "extra_ports": ports[1:],
                "kind": kind,
                "pid": proc.pid,
                "uptime_seconds": None,
                "already_registered": existing is not None,
                "registered_as": existing["name"] if existing else None,
                "note": note,
                "importable": importable,
            }
        )

    order = {"project": 0, "tool": 1, "service": 2}
    return sorted(candidates, key=lambda c: (order.get(c["kind"], 3), c["port"] or 0))
