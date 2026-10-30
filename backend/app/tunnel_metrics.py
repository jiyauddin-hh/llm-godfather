"""Measure what the Cloudflare tunnels are actually carrying.

cloudflared publishes Prometheus counters on the local metrics port God Father
pins for each tunnel. Counters only climb, so nothing here is a rate until two
readings have been taken and subtracted — which is why a sampler runs on a
timer and the readings are kept, rather than the dashboard reading the endpoint
directly and showing a number that means nothing.

What can be measured this way is everything that passes *through* the tunnel:
requests, response codes, errors, bytes, round-trip time to the Cloudflare edge
and how long the local application takes to answer. What cannot is anything
Cloudflare decided before the traffic reached us — visitor countries, unique
visitors, cache hits, blocked bots. Those live in Cloudflare's own analytics API
and need a real zone, so they do not exist at all for a quick tunnel.
"""

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from .database import db, utcnow

# How often each live tunnel is read, and how long readings are kept.
SAMPLE_SECONDS = 10.0
RETENTION_HOURS = 24

# The metrics that matter, and what they mean in the dashboard.
WANTED = {
    "cloudflared_tunnel_total_requests": "requests",
    "cloudflared_tunnel_request_errors": "errors",
    "cloudflared_tunnel_concurrent_requests_per_tunnel": "concurrent",
    "cloudflared_tunnel_ha_connections": "connections",
}

# Summed across connections — cloudflared reports these per QUIC connection.
SUMMED = {
    "quic_client_sent_bytes": "bytes_sent",
    "quic_client_receive_bytes": "bytes_received",
}

FETCH_TIMEOUT = 3.0


# ------------------------------------------------------------------ parsing


def _split_labels(raw: str) -> dict[str, str]:
    """Pull `{a="1",b="2"}` apart. Values here never contain commas or quotes."""
    labels: dict[str, str] = {}
    for part in raw.split(","):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        labels[key.strip()] = value.strip().strip('"')
    return labels


def parse(text: str) -> dict:
    """Reduce a Prometheus exposition page to the handful of figures we show."""
    out: dict = {
        "requests": 0,
        "errors": 0,
        "concurrent": 0,
        "connections": 0,
        "bytes_sent": 0,
        "bytes_received": 0,
        "rtt_ms": 0.0,
        "edge_location": "",
        "hostname": "",
        "codes": {},
    }
    rtts: list[float] = []

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        name, _, remainder = line.partition("{")
        if remainder:
            label_text, _, value_text = remainder.partition("}")
            labels = _split_labels(label_text)
        else:
            parts = line.split()
            if len(parts) < 2:
                continue
            name, value_text, labels = parts[0], parts[1], {}

        try:
            value = float(value_text.strip())
        except ValueError:
            continue

        if name in WANTED:
            out[WANTED[name]] = int(value)
        elif name in SUMMED:
            out[SUMMED[name]] += int(value)
        elif name == "cloudflared_tunnel_response_by_code":
            code = labels.get("status_code")
            if code:
                out["codes"][code] = out["codes"].get(code, 0) + int(value)
        elif name == "cloudflared_tunnel_server_locations" and value > 0:
            out["edge_location"] = labels.get("edge_location", "")
        elif name == "cloudflared_tunnel_user_hostnames_counts" and value > 0:
            out["hostname"] = labels.get("userHostname", "")
        elif name == "quic_client_smoothed_rtt":
            rtts.append(value)

    if rtts:
        out["rtt_ms"] = round(sum(rtts) / len(rtts), 2)
    return out


def read(metrics_port: int) -> dict | None:
    """Read one tunnel's metrics endpoint. None if it is not answering."""
    url = f"http://127.0.0.1:{metrics_port}/metrics"
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as response:
            if response.status != 200:
                return None
            return parse(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


# ----------------------------------------------------------------- storing


def _store(route_id: int, reading: dict) -> None:
    with db() as conn:
        conn.execute(
            """INSERT INTO tunnel_samples
                   (route_id, ts, requests, errors, concurrent, connections,
                    bytes_sent, bytes_received, rtt_ms, edge_location, codes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                route_id,
                utcnow(),
                reading["requests"],
                reading["errors"],
                reading["concurrent"],
                reading["connections"],
                reading["bytes_sent"],
                reading["bytes_received"],
                reading["rtt_ms"],
                reading["edge_location"],
                json.dumps(reading["codes"]),
            ),
        )


def sweep() -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=RETENTION_HOURS)).isoformat(
        timespec="seconds"
    )
    with db() as conn:
        conn.execute("DELETE FROM tunnel_samples WHERE ts < ?", (cutoff,))


def sample_once() -> int:
    """Read every live tunnel once. Returns how many answered."""
    from .cloudflare import routes

    taken = 0
    for item in routes():
        if not item["enabled"] or not item["metrics_port"]:
            continue
        reading = read(item["metrics_port"])
        if reading is None:
            continue
        _store(item["id"], reading)
        taken += 1

        # The metrics page names the tunnel's public address, which is a
        # steadier source than the log banner: it survives rotation and is
        # still there long after the line that announced it has scrolled away.
        if reading["hostname"] and reading["hostname"] != item["public_url"]:
            with db() as conn:
                conn.execute(
                    "UPDATE tunnel_routes SET public_url = ?, updated_at = ? WHERE id = ?",
                    (reading["hostname"], utcnow(), item["id"]),
                )
    return taken


# ---------------------------------------------------------------- the loop


class Sampler:
    """Reads every live tunnel on a timer, in one background thread."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_run: str = ""
        self.last_count: int = 0

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
        sweeps = 0
        while not self._stop.is_set():
            try:
                self.last_count = sample_once()
                self.last_run = utcnow()
            except Exception:
                # A sampler that dies takes the dashboard with it, and no
                # reading is worth that.
                pass

            sweeps += 1
            # Roughly every ten minutes at the default interval.
            if sweeps >= 60:
                sweeps = 0
                try:
                    sweep()
                except Exception:
                    pass

            self._stop.wait(SAMPLE_SECONDS)


sampler = Sampler()


# ---------------------------------------------------------------- querying


def _rows(route_id: int, minutes: int) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(
        timespec="seconds"
    )
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM tunnel_samples WHERE route_id = ? AND ts >= ? ORDER BY id",
            (route_id, since),
        ).fetchall()
    return [dict(row) for row in rows]


def _delta(current: int, previous: int) -> int:
    """Counter difference, treating a drop as a restart rather than a negative.

    cloudflared starts counting from zero again every time it restarts, and
    auto-restart makes that a routine event rather than a rare one. Subtracting
    blindly would draw a deep negative spike across the chart at exactly the
    moment something interesting happened.
    """
    if current < previous:
        return current
    return current - previous


def series(route_id: int, minutes: int, points: int = 60) -> dict:
    """Rates over the window, plus the totals accumulated inside it."""
    rows = _rows(route_id, minutes)
    empty = {
        "points": [],
        "requests": 0,
        "errors": 0,
        "bytes_sent": 0,
        "bytes_received": 0,
        "peak_rpm": 0.0,
    }
    if len(rows) < 2:
        return empty

    steps = []
    for previous, current in zip(rows, rows[1:]):
        gap = _seconds_between(previous["ts"], current["ts"])
        if gap <= 0:
            continue
        steps.append(
            {
                "ts": current["ts"],
                "requests": _delta(current["requests"], previous["requests"]),
                "errors": _delta(current["errors"], previous["errors"]),
                "bytes_sent": _delta(current["bytes_sent"], previous["bytes_sent"]),
                "bytes_received": _delta(current["bytes_received"], previous["bytes_received"]),
                "concurrent": current["concurrent"],
                "rtt_ms": current["rtt_ms"],
                "gap": gap,
            }
        )

    if not steps:
        return empty

    # Fold into at most `points` buckets so a 24-hour window is still one chart.
    size = max(1, len(steps) // points + (1 if len(steps) % points else 0))
    buckets = []
    for index in range(0, len(steps), size):
        chunk = steps[index : index + size]
        span = sum(step["gap"] for step in chunk) or 1
        requests = sum(step["requests"] for step in chunk)
        buckets.append(
            {
                "ts": chunk[-1]["ts"],
                "rpm": round(requests * 60.0 / span, 2),
                "errors": sum(step["errors"] for step in chunk),
                "bytes_sent": sum(step["bytes_sent"] for step in chunk),
                "bytes_received": sum(step["bytes_received"] for step in chunk),
                "concurrent": max(step["concurrent"] for step in chunk),
                "rtt_ms": round(sum(step["rtt_ms"] for step in chunk) / len(chunk), 2),
            }
        )

    return {
        "points": buckets,
        "requests": sum(step["requests"] for step in steps),
        "errors": sum(step["errors"] for step in steps),
        "bytes_sent": sum(step["bytes_sent"] for step in steps),
        "bytes_received": sum(step["bytes_received"] for step in steps),
        "peak_rpm": max((bucket["rpm"] for bucket in buckets), default=0.0),
    }


def latest(route_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM tunnel_samples WHERE route_id = ? ORDER BY id DESC LIMIT 1",
            (route_id,),
        ).fetchone()
    if row is None:
        return None
    item = dict(row)
    try:
        item["codes"] = json.loads(item["codes"])
    except (ValueError, TypeError):
        item["codes"] = {}
    return item


def codes_in_window(route_id: int, minutes: int) -> dict[str, int]:
    """Responses by status code accumulated over the window."""
    rows = _rows(route_id, minutes)
    if len(rows) < 2:
        return {}

    def loads(raw: str) -> dict[str, int]:
        try:
            return {str(k): int(v) for k, v in json.loads(raw).items()}
        except (ValueError, TypeError, AttributeError):
            return {}

    totals: dict[str, int] = {}
    for previous, current in zip(rows, rows[1:]):
        before, after = loads(previous["codes"]), loads(current["codes"])
        for code, value in after.items():
            totals[code] = totals.get(code, 0) + _delta(value, before.get(code, 0))
    return {code: value for code, value in sorted(totals.items()) if value}


def _seconds_between(first: str, second: str) -> float:
    try:
        return (datetime.fromisoformat(second) - datetime.fromisoformat(first)).total_seconds()
    except (ValueError, TypeError):
        return 0.0
