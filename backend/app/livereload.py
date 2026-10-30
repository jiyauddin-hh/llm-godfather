"""Watch an app's source tree and refresh its wall tile when it changes.

God Father launches applications with no reloader of its own, so an edit ends
up stale in two different places at once: the running process still holds the
code it imported at startup, and the wall's iframe still shows the page it
loaded minutes ago. Fixing only one of them leaves the other lying, so both are
handled here — a server-side edit restarts the process, and *every* edit bumps
a revision number the wall watches to reload that one tile.

Polling rather than inotify: the standard library gives us no watcher, a
project tree with the noise directories pruned is a few thousand cheap stat
calls, and a second of latency is imperceptible next to the restart it sets
off. It also means no new dependency for a feature that is off by default.
"""

import os
import threading
import time
from pathlib import Path, PurePath

from .database import record_event
from .news import news
from .manager import manager
from .ports import port_is_listening
from .service import restart_process

# How often the tree is re-scanned, and how long it must be quiet afterwards.
# Saving a file often writes it two or three times; the quiet window collapses
# that burst, and a formatter touching thirty files, into a single reload.
POLL_SECONDS = 1.0
QUIET_SECONDS = 0.8

# A restarted app needs its port back before the tile is worth reloading —
# reload too early and the frame shows a connection error instead of the change.
PORT_WAIT_SECONDS = 15.0
SETTLE_SECONDS = 0.4

# Walking these is pure cost: they are enormous, machine-written, and none of
# it is what the user just edited.
IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache", ".tox", ".idea",
    ".vscode", "site-packages", ".next", ".nuxt", ".svelte-kit", ".parcel-cache",
    "coverage", "htmlcov", "target", "build", "vendor", ".gradle", ".terraform",
}

# Editor scratch files, compiled artefacts and logs the app writes to itself.
# Without the log rule an app that logs a request would reload the tile, which
# would log another request, forever.
IGNORED_SUFFIXES = {
    ".pyc", ".pyo", ".pyd", ".swp", ".swo", ".swx", ".tmp", ".temp", ".bak",
    ".log", ".lock", ".pid", ".sock", ".db-journal", ".sqlite-journal",
    ".sqlite3-journal", ".md", ".rst", ".txt",
}
IGNORED_PREFIXES = (".#", "~$", ".goutputstream", ".~lock")

# A page, a stylesheet or a bundle is read off disk on the request that asks
# for it, so the running process is already serving the new one — reloading the
# frame is the whole fix. Source the interpreter imported once is not.
CLIENT_DIRS = {
    "static", "public", "assets", "www", "dist", "templates", "media",
    "img", "images", "css", "js", "styles", "scripts", "fonts",
}
CLIENT_SUFFIXES = {
    ".html", ".htm", ".css", ".scss", ".sass", ".less", ".svg", ".png", ".jpg",
    ".jpeg", ".gif", ".webp", ".avif", ".ico", ".woff", ".woff2", ".ttf",
    ".eot", ".map",
}

# A tree bigger than this is not a project, it is a home directory that was
# typed into the working-directory box. Watch what we have seen and say so
# rather than burning a core on every scan.
MAX_FILES = 20_000


def _should_skip(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith(IGNORED_PREFIXES) or os.path.splitext(lowered)[1] in IGNORED_SUFFIXES


def snapshot(root: Path) -> tuple[dict[str, float], bool]:
    """Map every watchable file under `root` to its mtime.

    Returns the map and whether the walk was cut short by MAX_FILES.
    """
    files: dict[str, float] = {}
    truncated = False
    stack = [root]

    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue

        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in IGNORED_DIRS:
                        stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    if _should_skip(entry.name):
                        continue
                    if len(files) >= MAX_FILES:
                        return files, True
                    files[entry.path] = entry.stat().st_mtime
            except OSError:
                continue

    return files, truncated


def needs_restart(relative: PurePath) -> bool:
    """Would the running process still serve the old version of this file?"""
    if relative.suffix.lower() in CLIENT_SUFFIXES:
        return False
    if any(part.lower() in CLIENT_DIRS for part in relative.parts[:-1]):
        return False
    return True


class _Watch:
    """One app's tree, its last known state, and the change waiting to be acted on."""

    def __init__(self, app_id: int, name: str, root: Path):
        self.app_id = app_id
        self.name = name
        self.root = root
        self.files, self.truncated = snapshot(root)
        self.pending: set[str] = set()
        self.pending_restart = False
        self.last_change: float = 0.0

    def diff(self) -> set[str]:
        """Paths added, removed or rewritten since the last scan."""
        current, truncated = snapshot(self.root)
        previous, self.files = self.files, current
        changed = {path for path, mtime in current.items() if previous.get(path) != mtime}

        # A capped walk stops at an arbitrary point, so which files it reached
        # shifts between scans. Reading that as a deletion would reload the
        # tile on a loop, forever, over a tree nobody edited.
        if not (truncated or self.truncated):
            changed |= set(previous) - set(current)

        self.truncated = truncated
        return changed


class LiveReload:
    """Watches the opted-in apps and publishes a revision per app.

    The revision is the whole contract with the wall: the dashboard subscribes
    to it over a WebSocket for an instant reload, and the read-only network
    wall picks it up from the tile data it already polls. Neither needs a route
    that can change anything.
    """

    def __init__(self) -> None:
        self._watches: dict[int, _Watch] = {}
        self._revisions: dict[int, int] = {}
        self._subscribers: set = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._loop = None

    # ------------------------------------------------------------ lifecycle

    def bind_loop(self, loop) -> None:
        self._loop = loop

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="livereload", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=3)

    # -------------------------------------------------------------- watches

    def watch(self, app_id: int, name: str, working_dir: str) -> None:
        """Begin watching an app, or re-point an existing watch at a new directory."""
        root = Path(working_dir).expanduser()
        if not root.is_dir():
            return

        with self._lock:
            existing = self._watches.get(app_id)
            if existing is not None and existing.root == root:
                existing.name = name
                return
            watch = _Watch(app_id, name, root)
            self._watches[app_id] = watch

        if watch.truncated:
            self._say(app_id, f"live reload: {root} holds more than {MAX_FILES:,} files — watching the first {MAX_FILES:,}")
        self.start()

    def unwatch(self, app_id: int) -> None:
        with self._lock:
            self._watches.pop(app_id, None)

    def is_watching(self, app_id: int) -> bool:
        with self._lock:
            return app_id in self._watches

    def revision(self, app_id: int) -> int:
        with self._lock:
            return self._revisions.get(app_id, 0)

    # ---------------------------------------------------------- subscribers

    def subscribe(self):
        import asyncio

        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue) -> None:
        self._subscribers.discard(queue)

    def _publish(self, message: dict) -> None:
        loop = self._loop
        for queue in list(self._subscribers):
            try:
                if loop is not None:
                    loop.call_soon_threadsafe(queue.put_nowait, message)
            except (RuntimeError, Exception):
                # A full queue or a closed loop must never stall the watcher.
                pass

    # ------------------------------------------------------------- the loop

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                # A watcher that dies silently is worse than one that skips a
                # scan, so swallow whatever one bad tree threw and carry on.
                pass
            self._stop.wait(POLL_SECONDS)

    def _tick(self) -> None:
        with self._lock:
            watches = list(self._watches.values())

        now = time.monotonic()
        for watch in watches:
            try:
                changed = watch.diff()
            except OSError:
                continue

            if changed:
                watch.last_change = now
                for path in changed:
                    try:
                        relative = PurePath(path).relative_to(watch.root)
                    except ValueError:
                        relative = PurePath(path)
                    watch.pending.add(str(relative))
                    if needs_restart(relative):
                        watch.pending_restart = True

            # Wait for the writing to stop before acting on it.
            if watch.pending and now - watch.last_change >= QUIET_SECONDS:
                files = sorted(watch.pending)
                restart = watch.pending_restart
                watch.pending = set()
                watch.pending_restart = False
                self._apply(watch, files, restart)

    def _apply(self, watch: _Watch, files: list[str], restart: bool) -> None:
        summary = files[0] if len(files) == 1 else f"{files[0]} and {len(files) - 1} more"
        process = manager.peek(watch.app_id)
        owned = process is not None and process.is_alive

        if restart and owned:
            self._say(watch.app_id, f"live reload: {summary} changed — restarting")
            try:
                port = restart_process(watch.app_id)
            except Exception as exc:
                # HTTPException carries the readable message in .detail.
                reason = getattr(exc, "detail", None) or str(exc)
                self._say(watch.app_id, f"live reload: restart failed — {reason}")
                record_event(watch.app_id, "error", f"live reload: {reason}")
                self._bump(watch, summary, restarted=False, ok=False)
                return
            record_event(watch.app_id, "restarted", f"live reload: {summary}")
            self._wait_for_port(port)
        elif restart:
            # Started from a terminal, so its process is not ours to recycle.
            self._say(watch.app_id, f"live reload: {summary} changed — reloading the tile (restart it to load new server code)")
        else:
            self._say(watch.app_id, f"live reload: {summary} changed — reloading the tile")

        if not (restart and owned):
            # A restart is already news through its event; a tile reload is not an event.
            news.for_app(watch.app_id, "info", "event", f"Page updated — {summary} changed")

        self._bump(watch, summary, restarted=restart and owned, ok=True)

    def _bump(self, watch: _Watch, summary: str, restarted: bool, ok: bool) -> None:
        with self._lock:
            revision = self._revisions.get(watch.app_id, 0) + 1
            self._revisions[watch.app_id] = revision
        self._publish(
            {
                "app_id": watch.app_id,
                "name": watch.name,
                "rev": revision,
                "changed": summary,
                "restarted": restarted,
                "ok": ok,
            }
        )

    def _wait_for_port(self, port: int | None) -> None:
        """Hold the reload back until the app answers again."""
        if not port:
            time.sleep(SETTLE_SECONDS)
            return
        deadline = time.monotonic() + PORT_WAIT_SECONDS
        while time.monotonic() < deadline:
            if port_is_listening(port):
                time.sleep(SETTLE_SECONDS)
                return
            time.sleep(0.2)

    def _say(self, app_id: int, text: str) -> None:
        """Put a line in the app's own log, where the user is already looking."""
        try:
            manager.get(app_id).emit("system", text)
        except Exception:
            pass


live_reload = LiveReload()
