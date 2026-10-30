"""The news ticker: one running feed of what just happened, across every app.

Four sources feed it, and each is the cheapest honest signal for its case:

*Lifecycle events.* Every start, stop, crash and restart already goes through
`record_event`, so the ticker hears about them there rather than from a second
set of hooks that could drift.

*The app's own announcement.* A line printed as `[news] Payment received` (or
`[news:good]`, `[news:warn]`, `[news:error]`) is lifted out of the log as-is,
and an app God Father does not run can POST the same thing to /api/news. This
is the only source that knows what an event *means* — a log line says
"INSERT INTO payments", only the app can say "₹25,000 from Priya".

*Log lines worth reading.* Errors, warnings, HTTP 5xx responses, and any line
containing one of the app's own keywords. Rate-limited and de-duplicated, since
a crash loop prints the same traceback a hundred times and a ticker that shows
it a hundred times is a ticker nobody reads.

*Apps running outside God Father.* Their output is not ours, so the only thing
we can see is their port going quiet or coming back. A slow poll watches that.
"""

import asyncio
import re
import sqlite3
import threading
import time
from collections import deque

from .database import db, utcnow

LEVELS = ("good", "info", "warn", "error")

# How many items the feed keeps. The ticker shows far fewer; the rest is what
# a page opened later can catch up on.
KEEP = 500

# A line longer than this is a stack dump or a JSON blob, not a headline.
MAX_MESSAGE = 220

# Log-derived news per app: at most this many in a rolling minute, and the same
# text (digits ignored) at most once per quiet window.
LOG_PER_MINUTE = 6
REPEAT_WINDOW = 300.0

# App-sent news is trusted more, but a loop printing [news] still gets capped.
ANNOUNCE_PER_MINUTE = 30

# Per-app settings are read on every log line, so they are cached this long.
SETTINGS_TTL = 5.0

# The "All logs" tape: every captured line from every app, held in memory only.
# Enough for a page opened now to have something to show, and for the phone
# page to catch up between polls. Not stored — the Logs tab is the history.
LOG_RING = 500

# The same event for the same app inside this window is one headline. Autostart
# records its own "started" right after the process manager has recorded one.
EVENT_REPEAT_SECONDS = 5.0

# Port watch for apps started outside God Father.
WATCH_SECONDS = 15.0

ANNOUNCE = re.compile(r"^\s*\[news(?::(good|info|warn|error))?\]\s*(.+)$", re.I)
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
LOG_PREFIX = re.compile(
    r"^(?:\[?\d{4}-\d\d-\d\d[T ][\d:.,]+(?:Z|[+-]\d\d:?\d\d)?\]?\s*)?"
    r"(?:\[?(?:INFO|DEBUG|TRACE|WARN(?:ING)?|ERROR|CRITICAL|FATAL)\]?:?\s*)?",
    re.I,
)
ERROR = re.compile(
    r"\b(?:error|exception|fatal|critical|panic|unhandled|segmentation fault)\b"
    r"|\b\w+(?:Error|Exception):",
    re.I,
)
WARNING = re.compile(r"\bwarn(?:ing)?\b|\bdeprecat", re.I)
# Carries no information on its own; the line that names the exception does.
NOISE = re.compile(r"^\s*(?:Traceback \(most recent call last\)|File \"|at |\^+\s*$)")
DIGITS = re.compile(r"\d+")

EVENT_NEWS = {
    "started": ("good", "Started"),
    "stopped": ("info", "Stopped"),
    "crashed": ("error", "Crashed"),
    "error": ("error", "Error"),
    "restarted": ("warn", "Restarted"),
    "created": ("info", "Added to God Father"),
    "shared": ("info", "Shared on the network"),
    "unshared": ("info", "No longer shared on the network"),
    "killed": ("warn", "Process killed"),
    "takeover": ("warn", "Taken over by God Father"),
}


def clean(text: str) -> str:
    text = ANSI.sub("", text).strip()
    if len(text) > MAX_MESSAGE:
        text = text[: MAX_MESSAGE - 1].rstrip() + "…"
    return text


def parse_keywords(raw: str) -> list[str]:
    return [word.strip().lower() for word in (raw or "").split(",") if word.strip()]


class _AppState:
    """Rate limiting for one app's log-derived news."""

    def __init__(self) -> None:
        self.recent: deque[float] = deque()
        self.announced: deque[float] = deque()
        self.seen: dict[str, float] = {}

    @staticmethod
    def _allow(stamps: deque[float], limit: int, now: float) -> bool:
        while stamps and now - stamps[0] > 60:
            stamps.popleft()
        if len(stamps) >= limit:
            return False
        stamps.append(now)
        return True

    def allow_log(self, text: str, now: float) -> bool:
        key = DIGITS.sub("#", text.lower())
        last = self.seen.get(key)
        if last is not None and now - last < REPEAT_WINDOW:
            return False
        if not self._allow(self.recent, LOG_PER_MINUTE, now):
            return False
        self.seen[key] = now
        if len(self.seen) > 200:
            cutoff = now - REPEAT_WINDOW
            self.seen = {k: v for k, v in self.seen.items() if v > cutoff}
        return True

    def allow_announce(self, now: float) -> bool:
        return self._allow(self.announced, ANNOUNCE_PER_MINUTE, now)


class NewsFeed:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        self._states: dict[int, _AppState] = {}
        self._settings: dict[int, dict] = {}
        self._settings_at = 0.0
        self._inserts = 0
        self._lines: deque[dict] = deque(maxlen=LOG_RING)
        self._line_seq = 0
        self._line_subscribers: set[asyncio.Queue] = set()
        self._last_event: dict[tuple[int, str], float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ------------------------------------------------------------ settings

    def _app(self, app_id: int) -> dict | None:
        now = time.monotonic()
        with self._lock:
            if now - self._settings_at > SETTINGS_TTL:
                try:
                    with db() as conn:
                        rows = conn.execute(
                            "SELECT id, name, port, news_enabled, news_keywords FROM apps"
                        ).fetchall()
                    self._settings = {
                        row["id"]: {
                            "name": row["name"],
                            "port": row["port"],
                            "enabled": bool(row["news_enabled"]),
                            "keywords": parse_keywords(row["news_keywords"]),
                        }
                        for row in rows
                    }
                    self._settings_at = now
                except sqlite3.Error:
                    pass
            return self._settings.get(app_id)

    def invalidate(self) -> None:
        """Forget cached settings, so an edit applies to the very next line."""
        with self._lock:
            self._settings_at = 0.0

    def _state(self, app_id: int) -> _AppState:
        with self._lock:
            return self._states.setdefault(app_id, _AppState())

    # ------------------------------------------------------------- publish

    def publish(self, app_id: int | None, app_name: str, level: str, source: str, message: str) -> dict | None:
        """Store one item and push it to every open ticker. Never raises."""
        message = clean(message)
        if not message:
            return None
        if level not in LEVELS:
            level = "info"

        item = {
            "app_id": app_id,
            "app_name": app_name,
            "level": level,
            "source": source,
            "message": message,
            "created_at": utcnow(),
        }
        try:
            with db() as conn:
                cursor = conn.execute(
                    """INSERT INTO news (app_id, app_name, level, source, message, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (app_id, app_name, level, source, message, item["created_at"]),
                )
                item["id"] = cursor.lastrowid
                self._inserts += 1
                if self._inserts % 50 == 0:
                    conn.execute(
                        "DELETE FROM news WHERE id <= (SELECT MAX(id) FROM news) - ?", (KEEP,)
                    )
        except sqlite3.Error:
            return None

        self._push(self._subscribers, item)
        return item

    @staticmethod
    def _offer(queue: asyncio.Queue, item: dict) -> None:
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            pass

    def for_app(self, app_id: int, level: str, source: str, message: str) -> dict | None:
        """Publish about one app, unless it has been taken off the ticker."""
        app = self._app(app_id)
        if app is None or not app["enabled"]:
            return None
        return self.publish(app_id, app["name"], level, source, message)

    def from_event(self, app_id: int, type_: str, message: str) -> None:
        """Turn a lifecycle event into a headline, if it is one worth showing."""
        mapped = EVENT_NEWS.get(type_)
        if mapped is None:
            return
        app = self._app(app_id)
        if app is None or not app["enabled"]:
            return
        now = time.monotonic()
        with self._lock:
            last = self._last_event.get((app_id, type_))
            self._last_event[(app_id, type_)] = now
        if last is not None and now - last < EVENT_REPEAT_SECONDS:
            return
        level, headline = mapped
        # "pid 1234" is bookkeeping, not news; keep the reason when there is one.
        detail = "" if type_ in {"started", "created"} and message.startswith(("pid ", app["name"])) else message
        self.publish(app_id, app["name"], level, "event", f"{headline} — {detail}" if detail else headline)

    def _push(self, subscribers: set[asyncio.Queue], item: dict) -> None:
        loop = self._loop
        if loop is None:
            return
        for queue in list(subscribers):
            try:
                loop.call_soon_threadsafe(self._offer, queue, item)
            except RuntimeError:
                # Event loop already closed during shutdown.
                pass

    def _raw(self, app_id: int, stream: str, text: str) -> None:
        """Put every line on the All logs tape, coloured by what it looks like."""
        app = self._app(app_id)
        if app is None or not app["enabled"]:
            return
        message = clean(text)
        if not message:
            return

        announced = ANNOUNCE.match(message)
        if announced:
            level, message = (announced.group(1) or "good").lower(), announced.group(2)
        elif ERROR.search(message):
            level = "error"
        elif WARNING.search(message):
            level = "warn"
        elif app["keywords"] and any(word in message.lower() for word in app["keywords"]):
            level = "good"
        else:
            level = "info"

        with self._lock:
            self._line_seq += 1
            item = {
                "id": self._line_seq,
                "app_id": app_id,
                "app_name": app["name"],
                "level": level,
                "stream": stream,
                "message": message,
                "created_at": utcnow(),
            }
            self._lines.append(item)
        self._push(self._line_subscribers, item)

    def lines(self, limit: int = 40, after: int = 0) -> list[dict]:
        """Newest first, like recent(). `after` returns only lines newer than that id."""
        limit = max(1, min(limit, LOG_RING))
        with self._lock:
            newer = [item for item in self._lines if item["id"] > after]
        return newer[-limit:][::-1]

    def subscribe_lines(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._line_subscribers.add(queue)
        return queue

    def unsubscribe_lines(self, queue: asyncio.Queue) -> None:
        self._line_subscribers.discard(queue)

    def from_line(self, app_id: int, stream: str, text: str) -> None:
        """Pick the news out of one captured log line. Called for every line."""
        self._raw(app_id, stream, text)
        if stream == "system":
            return
        now = time.monotonic()

        announced = ANNOUNCE.match(text)
        if announced:
            app = self._app(app_id)
            if app and app["enabled"] and self._state(app_id).allow_announce(now):
                self.publish(app_id, app["name"], (announced.group(1) or "info").lower(), "app", announced.group(2))
            return

        # Most lines are none of these; decide that before touching settings.
        if NOISE.match(text):
            return
        level = None
        request = None
        if "HTTP/" in text or " /" in text:
            from .traffic import parse_line

            request = parse_line(text)
        if request is not None:
            if request["status"] >= 500:
                level = "error"
        elif ERROR.search(text):
            level = "error"
        elif WARNING.search(text):
            level = "warn"

        app = self._app(app_id)
        if app is None or not app["enabled"]:
            return
        if level is None and app["keywords"]:
            lowered = text.lower()
            if any(word in lowered for word in app["keywords"]):
                level = "good"
        if level is None:
            return

        if request is not None and level == "error":
            message = f"HTTP {request['status']} on {request['method']} {request['path']}"
        elif request is not None:
            message = f"{request['method']} {request['path']} ({request['status']})"
        else:
            message = LOG_PREFIX.sub("", clean(text)) or clean(text)
        if self._state(app_id).allow_log(message, now):
            self.publish(app_id, app["name"], level, "log", message)

    def forget(self, app_id: int) -> None:
        with self._lock:
            self._states.pop(app_id, None)
        self.invalidate()

    # ------------------------------------------------------------- reading

    def recent(self, limit: int = 50, after: int = 0) -> list[dict]:
        limit = max(1, min(limit, KEEP))
        try:
            with db() as conn:
                rows = conn.execute(
                    "SELECT * FROM news WHERE id > ? ORDER BY id DESC LIMIT ?", (after, limit)
                ).fetchall()
        except sqlite3.Error:
            return []
        return [dict(row) for row in rows]

    def clear(self) -> None:
        with db() as conn:
            conn.execute("DELETE FROM news")

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    # -------------------------------------------------- apps run elsewhere

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, name="news-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _watch(self) -> None:
        """Report apps we do not run going quiet or coming back.

        Apps we run already report through their lifecycle events, so they are
        tracked as 'owned' and never announced from here — otherwise every
        stop would be news twice.
        """
        from .manager import manager
        from .ports import is_port_open, listening_ports

        previous: dict[int, str] = {}
        while not self._stop.wait(WATCH_SECONDS):
            try:
                self.invalidate()
                self._app(0)
                with self._lock:
                    apps = dict(self._settings)
                listening = {entry["port"] for entry in listening_ports()}
            except Exception:
                continue

            current: dict[int, str] = {}
            for app_id, app in apps.items():
                port = app["port"]
                if not port:
                    continue
                process = manager.peek(app_id)
                if process is not None and process.is_alive:
                    state = "owned"
                elif port in listening or is_port_open(port):
                    state = "up"
                else:
                    state = "down"
                current[app_id] = state

                before = previous.get(app_id)
                if not app["enabled"] or before is None or before == state:
                    continue
                if before == "up" and state == "down":
                    self.publish(app_id, app["name"], "warn", "watch", f"Stopped responding on port {port}")
                elif before == "down" and state == "up":
                    self.publish(app_id, app["name"], "good", "watch", f"Came up on port {port} (running outside God Father)")
            previous = current


news = NewsFeed()
