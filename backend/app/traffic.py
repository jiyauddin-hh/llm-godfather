"""Requests per application, counted from the access lines they already print.

God Father sits beside an application, not in front of it: a browser talking to
``127.0.0.1:4000`` never passes through here, so there is no proxy to count at.
What there *is* is the application's captured output — and nearly every web
server writes one line per request unless told not to. Uvicorn, Werkzeug,
``http.server``, gunicorn, morgan and gin all do, each in its own dialect. This
module reads those lines as they arrive and files them into per-minute buckets.

Consequences worth stating plainly, because the Analytics tab repeats them:

* An app whose server prints no access log (Vite, ``uvicorn --no-access-log``)
  is shown as **not counted**, never as zero. A zero would be a claim.
* An app running outside God Father has no captured log, so nothing to read.
* Response time exists only where the server prints it (morgan, gin). Uvicorn
  does not, so its apps get request and error counts but no latency.
* The wall's own reachability probe also lands in an app's access log. Each
  probe is noted here as it is sent and the matching line is dropped, so the
  wall does not show up as traffic to the apps it is watching. History read
  on the first pass predates that bookkeeping, so there every loopback GET of
  the tile's own URL is treated as a probe — it costs the odd genuine reload
  of that one page, and saves the tab from a week of the wall talking to
  itself at the top of every chart.

The read position is stored per app, so a restart carries on where it stopped
rather than counting the same lines twice. The very first pass back-fills from
whatever the log files still hold, since every line carries its own timestamp.
"""

import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from .config import log_path
from .database import db, utcnow

SAMPLE_SECONDS = 10.0
RETENTION_DAYS = 7
# Upper bound on what one pass reads from one file. A log that grew faster than
# this is caught up over the next few passes rather than in one long stall.
READ_CHUNK_BYTES = 8_000_000

# Response-time histogram bounds in milliseconds; one more bucket above the last.
# Buckets rather than raw samples so p95 survives being summed across minutes.
LATENCY_BOUNDS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)
HIST_COLUMNS = [f"h{index}" for index in range(len(LATENCY_BOUNDS) + 1)]

COUNTER_COLUMNS = [
    "requests", "s2xx", "s3xx", "s4xx", "s5xx",
    "loopback", "lan", "external", "bytes", "timed", "latency_sum",
] + HIST_COLUMNS


# ------------------------------------------------------------------- parsing

METHODS = r"GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|CONNECT|TRACE"

# Order matters: the specific dialects first, the loose ones last.
PATTERNS = [
    # gin:  [GIN] 2026/09/26 - 11:00:00 | 200 |   1.234ms |  127.0.0.1 | GET  "/path"
    re.compile(
        r"\|\s*(?P<status>\d{3})\s*\|\s*(?P<dur>[\d.]+)(?P<unit>µs|us|ms|s)\s*\|\s*"
        rf"(?P<ip>\S+)\s*\|\s*(?P<method>{METHODS})\s+\"(?P<path>[^\"]+)\""
    ),
    # uvicorn:  INFO:     127.0.0.1:57356 - "GET /api/jobs HTTP/1.1" 200 OK
    re.compile(
        rf"(?P<ip>[^\s\"]+):\d+ - \"(?P<method>{METHODS}) (?P<path>\S+) HTTP/[\d.]+\" (?P<status>\d{{3}})"
    ),
    # common/combined log (werkzeug, http.server, gunicorn, nginx):
    #   127.0.0.1 - - [26/Sep/2026 11:00:00] "GET / HTTP/1.1" 200 1234
    re.compile(
        rf"(?P<ip>[^\s\"\[]+) \S+ \S+ \[[^\]]+\] \"(?P<method>{METHODS}) (?P<path>\S+)[^\"]*\" "
        r"(?P<status>\d{3}) (?P<bytes>\d+|-)"
    ),
    # any quoted request line without a client (django runserver and friends)
    re.compile(
        rf"\"(?P<method>{METHODS}) (?P<path>\S+) HTTP/[\d.]+\" (?P<status>\d{{3}})(?: (?P<bytes>\d+))?"
    ),
    # morgan dev / tiny:  GET /api/users 200 12.345 ms - 1234   |   GET / 200 1234 - 3.4 ms
    re.compile(
        rf"(?:^|\s)(?P<method>{METHODS}) (?P<path>/\S*) (?P<status>\d{{3}})\b"
        r"(?:.*?(?P<dur>\d+(?:\.\d+)?) ?(?P<unit>ms)\b)?"
    ),
]

# Cheap pre-check so lines that cannot be a request skip the pattern list.
BARE_REQUEST = re.compile(rf"(?:^|\s)(?:{METHODS}) /")

UNIT_TO_MS = {"µs": 0.001, "us": 0.001, "ms": 1.0, "s": 1000.0}

# Path segments that are an identifier rather than a route, collapsed so
# /users/41 and /users/42 are one endpoint rather than a thousand.
ID_SEGMENT = re.compile(
    r"^(?:\d+|[0-9a-f]{8,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{20,})$",
    re.I,
)


def parse_line(text: str) -> dict | None:
    """One access-log line to {method, path, status, ip, ms, bytes}, or None."""
    if "HTTP/" not in text and "|" not in text and not BARE_REQUEST.search(text):
        return None
    for pattern in PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        groups = match.groupdict()
        latency = None
        if groups.get("dur"):
            try:
                latency = float(groups["dur"]) * UNIT_TO_MS.get(groups.get("unit") or "ms", 1.0)
            except ValueError:
                latency = None
        size = groups.get("bytes")
        return {
            "method": groups["method"],
            "path": groups["path"],
            "status": int(groups["status"]),
            "ip": groups.get("ip"),
            "ms": latency,
            "bytes": int(size) if size and size.isdigit() else 0,
        }
    return None


def normalise_path(raw: str) -> str:
    path = raw.split("?", 1)[0].split("#", 1)[0] or "/"
    if not path.startswith("/"):
        # An absolute-form request line (proxies) — keep only the path part.
        path = "/" + path.split("://", 1)[-1].split("/", 1)[-1] if "://" in path else "/" + path
    parts = [":id" if ID_SEGMENT.match(part) else part for part in path.split("/")]
    return "/".join(parts)[:120] or "/"


def client_class(ip: str | None) -> str:
    if not ip:
        return "loopback"  # a server that omits the client is almost always a dev server
    ip = ip.strip("[]")
    if ip.startswith("::ffff:"):
        ip = ip[7:]
    if ip in {"localhost", "::1"} or ip.startswith("127."):
        return "loopback"
    if (
        ip.startswith(("10.", "192.168.", "169.254.", "fe80:", "fc", "fd"))
        or re.match(r"^172\.(1[6-9]|2\d|3[01])\.", ip)
    ):
        return "lan"
    return "external"


def histogram_slot(ms: float) -> int:
    for index, bound in enumerate(LATENCY_BOUNDS):
        if ms <= bound:
            return index
    return len(LATENCY_BOUNDS)


# ------------------------------------------------------- the wall's own probe

_probes: deque[tuple[float, str]] = deque(maxlen=2000)
_probes_lock = threading.Lock()
PROBE_MATCH_BEFORE = 2.0
PROBE_MATCH_AFTER = 8.0
PROBE_KEEP_SECONDS = 180.0


def note_probe(path: str) -> None:
    """Called by the wall just before it probes, so the line it causes is not traffic."""
    with _probes_lock:
        _probes.append((time.time(), path or "/"))


def _consume_probe(path: str, stamp: float) -> bool:
    with _probes_lock:
        for index, (when, probed) in enumerate(_probes):
            if probed == path and when - PROBE_MATCH_BEFORE <= stamp <= when + PROBE_MATCH_AFTER:
                del _probes[index]
                return True
    return False


def _forget_old_probes() -> None:
    cutoff = time.time() - PROBE_KEEP_SECONDS
    with _probes_lock:
        while _probes and _probes[0][0] < cutoff:
            _probes.popleft()


# ------------------------------------------------------------------- reading


def _parse_stamp(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _minute_key(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M")


def _hour_key(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H")


class _Batch:
    """Counters for one pass over one app's new lines, flushed in one transaction."""

    def __init__(self, app_id: int, not_before: float | None, match_probes: bool,
                 probe_path: str | None = None) -> None:
        self.app_id = app_id
        self.not_before = not_before
        self.match_probes = match_probes
        # Back-fill only: the path the wall probes, dropped wholesale from loopback.
        self.probe_path = probe_path
        self.minutes: dict[str, dict] = {}
        self.paths: dict[tuple[str, str, str], list] = {}
        self.probes_dropped = 0

    def feed(self, raw: str) -> None:
        parts = raw.rstrip("\n").split("\t", 2)
        if len(parts) != 3 or parts[1] == "system":
            return
        stamp = _parse_stamp(parts[0])
        if stamp is None or (self.not_before and stamp < self.not_before):
            return
        hit = parse_line(parts[2])
        if hit is None:
            return

        origin = client_class(hit["ip"])
        if origin == "loopback" and hit["method"] == "GET" and (
            (self.match_probes and _consume_probe(hit["path"], stamp))
            or (self.probe_path is not None and hit["path"] == self.probe_path)
        ):
            self.probes_dropped += 1
            return

        bucket = self.minutes.setdefault(_minute_key(stamp), dict.fromkeys(COUNTER_COLUMNS, 0))
        bucket["requests"] += 1
        family = hit["status"] // 100
        if family in (2, 3, 4, 5):
            bucket[f"s{family}xx"] += 1
        bucket[origin] += 1
        bucket["bytes"] += hit["bytes"]
        if hit["ms"] is not None:
            bucket["timed"] += 1
            bucket["latency_sum"] += hit["ms"]
            bucket[HIST_COLUMNS[histogram_slot(hit["ms"])]] += 1

        key = (_hour_key(stamp), hit["method"], normalise_path(hit["path"]))
        entry = self.paths.setdefault(key, [0, 0, 0.0, 0])
        entry[0] += 1
        entry[1] += 1 if hit["status"] >= 400 else 0
        if hit["ms"] is not None:
            entry[2] += hit["ms"]
            entry[3] += 1

    def flush(self, conn) -> None:
        if self.minutes:
            columns = ", ".join(COUNTER_COLUMNS)
            marks = ", ".join("?" for _ in COUNTER_COLUMNS)
            updates = ", ".join(f"{name} = {name} + excluded.{name}" for name in COUNTER_COLUMNS)
            conn.executemany(
                f"""INSERT INTO traffic_minutes (app_id, minute, {columns}) VALUES (?, ?, {marks})
                    ON CONFLICT(app_id, minute) DO UPDATE SET {updates}""",
                [
                    (self.app_id, minute, *[bucket[name] for name in COUNTER_COLUMNS])
                    for minute, bucket in self.minutes.items()
                ],
            )
        if self.paths:
            conn.executemany(
                """INSERT INTO traffic_paths (app_id, hour, method, path, requests, errors, latency_sum, timed)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(app_id, hour, method, path) DO UPDATE SET
                     requests = requests + excluded.requests,
                     errors = errors + excluded.errors,
                     latency_sum = latency_sum + excluded.latency_sum,
                     timed = timed + excluded.timed""",
                [
                    (self.app_id, hour, method, path, *values)
                    for (hour, method, path), values in self.paths.items()
                ],
            )


def _read_from(path, offset: int, batch: _Batch) -> int:
    """Feed lines from offset onward; return the offset reached (whole lines only)."""
    with path.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read(READ_CHUNK_BYTES)
    if not chunk:
        return offset
    end = chunk.rfind(b"\n")
    if end < 0:
        # A partial line still being written; wait for the rest of it.
        return offset
    for raw in chunk[: end + 1].decode("utf-8", errors="replace").splitlines():
        if len(raw) <= 20000:
            batch.feed(raw)
    return offset + end + 1


def _probe_path(row) -> str | None:
    """The path the wall requests for this app, as it appears in a request line."""
    # Here rather than at the top: wall.py imports this module to note its probes.
    from .wall import preview_url_for

    url = preview_url_for(row["port"], row["preview_url"])
    if not url:
        return None
    parsed = urlparse(url)
    path = parsed.path or "/"
    return f"{path}?{parsed.query}" if parsed.query else path


def scan_app(app_id: int) -> int:
    """Read whatever this app's log gained since last time. Returns requests filed."""
    path = log_path(app_id)
    rotated = path.with_suffix(".log.1")

    with db() as conn:
        cursor = conn.execute(
            "SELECT inode, offset FROM traffic_cursor WHERE app_id = ?", (app_id,)
        ).fetchone()
        row = conn.execute("SELECT port, preview_url FROM apps WHERE id = ?", (app_id,)).fetchone()

    try:
        stat = path.stat()
    except OSError:
        stat = None

    if cursor is None:
        # First sight of this app: back-fill from what the files still hold.
        not_before = time.time() - RETENTION_DAYS * 86400
        batch = _Batch(app_id, not_before, match_probes=False,
                       probe_path=_probe_path(row) if row else None)
        if rotated.exists():
            try:
                size = rotated.stat().st_size
                offset = 0
                while offset < size:
                    reached = _read_from(rotated, offset, batch)
                    if reached == offset:
                        break
                    offset = reached
            except OSError:
                pass
        inode, offset = (stat.st_ino, 0) if stat else (0, 0)
    else:
        batch = _Batch(app_id, None, match_probes=True)
        inode, offset = cursor["inode"], cursor["offset"]
        if stat and stat.st_ino != inode:
            # Rotated since the last pass: finish the old file, then start the new one.
            try:
                if rotated.exists() and rotated.stat().st_ino == inode:
                    size = rotated.stat().st_size
                    while offset < size:
                        reached = _read_from(rotated, offset, batch)
                        if reached == offset:
                            break
                        offset = reached
            except OSError:
                pass
            inode, offset = stat.st_ino, 0
        elif stat and stat.st_size < offset:
            # Cleared from the Logs tab; the file starts again from nothing.
            offset = 0

    if stat:
        try:
            offset = _read_from(path, offset, batch)
        except OSError:
            pass

    with db() as conn:
        batch.flush(conn)
        conn.execute(
            """INSERT INTO traffic_cursor (app_id, inode, offset, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(app_id) DO UPDATE SET
                 inode = excluded.inode, offset = excluded.offset, updated_at = excluded.updated_at""",
            (app_id, inode, offset, utcnow()),
        )
    return sum(bucket["requests"] for bucket in batch.minutes.values())


# One reader at a time: the timer and a "scan now" racing from the same offset
# would each file the same lines.
_scan_lock = threading.Lock()


def scan_all() -> int:
    with db() as conn:
        ids = [row["id"] for row in conn.execute("SELECT id FROM apps")]
    total = 0
    with _scan_lock:
        for app_id in ids:
            try:
                total += scan_app(app_id)
            except Exception:
                continue
        _forget_old_probes()
    return total


def sweep() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    with db() as conn:
        conn.execute("DELETE FROM traffic_minutes WHERE minute < ?", (cutoff.strftime("%Y-%m-%dT%H:%M"),))
        conn.execute("DELETE FROM traffic_paths WHERE hour < ?", (cutoff.strftime("%Y-%m-%dT%H"),))


class TrafficMonitor:
    """Reads every app's new log lines on a timer."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_run: str = ""
        self.last_error: str = ""

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._thread = None

    def _loop(self) -> None:
        rounds = 0
        while not self._stop.is_set():
            try:
                scan_all()
                self.last_run = utcnow()
                self.last_error = ""
            except Exception as error:
                self.last_error = str(error)
            rounds += 1
            if rounds % max(1, int(3600 / SAMPLE_SECONDS)) == 1:
                try:
                    sweep()
                except Exception:
                    pass
            self._stop.wait(SAMPLE_SECONDS)


monitor = TrafficMonitor()


# ------------------------------------------------------------------ querying


def _p95(hist: list[int], max_seen: float | None = None) -> float | None:
    total = sum(hist)
    if not total:
        return None
    target = total * 0.95
    running = 0
    for index, count in enumerate(hist):
        running += count
        if running >= target:
            if index < len(LATENCY_BOUNDS):
                return float(LATENCY_BOUNDS[index])
            return float(max_seen or LATENCY_BOUNDS[-1])
    return None


def _empty_counts() -> dict:
    return dict.fromkeys(COUNTER_COLUMNS, 0)


def _figures(counts: dict, minutes: int) -> dict:
    """The derived numbers every card shows, from summed counters."""
    requests = counts["requests"]
    errors = counts["s4xx"] + counts["s5xx"]
    hist = [counts[name] for name in HIST_COLUMNS]
    return {
        "requests": requests,
        "rpm": round(requests / max(1, minutes), 2),
        "s2xx": counts["s2xx"],
        "s3xx": counts["s3xx"],
        "s4xx": counts["s4xx"],
        "s5xx": counts["s5xx"],
        "errors": errors,
        "error_rate": round(errors * 100.0 / requests, 2) if requests else 0.0,
        "server_error_rate": round(counts["s5xx"] * 100.0 / requests, 2) if requests else 0.0,
        "loopback": counts["loopback"],
        "lan": counts["lan"],
        "external": counts["external"],
        "bytes": counts["bytes"],
        "timed": counts["timed"],
        "avg_ms": round(counts["latency_sum"] / counts["timed"], 1) if counts["timed"] else None,
        "p95_ms": _p95(hist),
    }


def summary(minutes: int, points: int = 60) -> dict:
    """Everything the Analytics tab draws for one window, before process stats."""
    now = time.time()
    since_stamp = now - minutes * 60
    since = _minute_key(since_stamp)
    since_hour = _hour_key(since_stamp)
    recent = _minute_key(now - 5 * 60)
    step = minutes * 60 / points

    columns = ", ".join(f"SUM({name}) AS {name}" for name in COUNTER_COLUMNS)
    with db() as conn:
        per_app = {
            row["app_id"]: dict(row)
            for row in conn.execute(
                f"SELECT app_id, {columns}, MAX(minute) AS last_minute FROM traffic_minutes "
                "WHERE minute >= ? GROUP BY app_id",
                (since,),
            )
        }
        recent_rows = {
            row["app_id"]: row["requests"]
            for row in conn.execute(
                "SELECT app_id, SUM(requests) AS requests FROM traffic_minutes "
                "WHERE minute >= ? GROUP BY app_id",
                (recent,),
            )
        }
        ever = {
            row["app_id"]: row["last_minute"]
            for row in conn.execute(
                "SELECT app_id, MAX(minute) AS last_minute FROM traffic_minutes GROUP BY app_id"
            )
        }
        series_rows = conn.execute(
            "SELECT app_id, minute, requests, s4xx + s5xx AS errors FROM traffic_minutes WHERE minute >= ?",
            (since,),
        ).fetchall()
        path_rows = conn.execute(
            """SELECT app_id, method, path, SUM(requests) AS requests, SUM(errors) AS errors,
                      SUM(latency_sum) AS latency_sum, SUM(timed) AS timed
               FROM traffic_paths WHERE hour >= ?
               GROUP BY app_id, method, path""",
            (since_hour,),
        ).fetchall()

    def slot_of(minute: str) -> int:
        stamp = datetime.strptime(minute, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc).timestamp()
        # The window's first minute starts a little before since_stamp; it
        # belongs in the first slot rather than off the left edge.
        return min(points - 1, max(0, int((stamp - since_stamp) // step)))

    total_series = [[0, 0] for _ in range(points)]
    app_series: dict[int, list[int]] = {}
    by_hour = [0] * 24
    for row in series_rows:
        index = slot_of(row["minute"])
        total_series[index][0] += row["requests"]
        total_series[index][1] += row["errors"]
        app_series.setdefault(row["app_id"], [0] * points)[index] += row["requests"]
        local_hour = datetime.strptime(row["minute"], "%Y-%m-%dT%H:%M").replace(
            tzinfo=timezone.utc
        ).astimezone().hour
        by_hour[local_hour] += row["requests"]

    bucket_minutes = step / 60
    series = [
        {
            "ts": datetime.fromtimestamp(since_stamp + index * step, timezone.utc).isoformat(
                timespec="seconds"
            ),
            "requests": requests,
            "errors": errors,
            "rpm": round(requests / bucket_minutes, 2),
        }
        for index, (requests, errors) in enumerate(total_series)
    ]

    paths_by_app: dict[int, list[dict]] = {}
    for row in path_rows:
        paths_by_app.setdefault(row["app_id"], []).append(
            {
                "method": row["method"],
                "path": row["path"],
                "requests": row["requests"],
                "errors": row["errors"],
                "avg_ms": round(row["latency_sum"] / row["timed"], 1) if row["timed"] else None,
            }
        )
    for rows in paths_by_app.values():
        rows.sort(key=lambda item: item["requests"], reverse=True)

    totals = _empty_counts()
    apps = {}
    for app_id, row in per_app.items():
        counts = {name: row[name] or 0 for name in COUNTER_COLUMNS}
        for name in COUNTER_COLUMNS:
            totals[name] += counts[name]
        apps[app_id] = {
            **_figures(counts, minutes),
            "rpm_now": round(recent_rows.get(app_id, 0) / 5, 2),
            "spark": app_series.get(app_id, [0] * points),
            "top_paths": paths_by_app.get(app_id, [])[:6],
        }

    return {
        "totals": {
            **_figures(totals, minutes),
            "rpm_now": round(sum(recent_rows.values()) / 5, 2),
        },
        "apps": apps,
        "ever_counted": ever,
        "series": series,
        "bucket_minutes": round(bucket_minutes, 2),
        "by_hour": by_hour,
        "top_paths": sorted(
            (
                {**item, "app_id": app_id}
                for app_id, rows in paths_by_app.items()
                for item in rows
            ),
            key=lambda item: item["requests"],
            reverse=True,
        )[:10],
    }
