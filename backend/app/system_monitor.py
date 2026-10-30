"""What the machine itself is doing, and which application is responsible.

Three jobs, deliberately separated because they answer different questions:

* **Now** — a snapshot for the gauges. Cheap, taken on demand.
* **History** — samples written on a slower timer and kept for a day. This is
  what answers "when was VRAM in use and when was it RAM", because a gauge can
  only ever say *this second* and the interesting part is the handoff.
* **Events** — the log half. A threshold crossed is recorded once, along with
  whatever was consuming the resource at that moment, so looking back at a
  spike tells you what caused it rather than only that it happened.

Attribution walks each registered application's process tree, so `npm start`
is charged for the node process it spawned. Everything else is reported by
process name and rolled into a top-N list; sampling all ~700 processes on a
timer would make the monitor a cause of the load it is meant to measure.
"""

import json
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

from .database import db, utcnow

try:
    import psutil
except ImportError:
    psutil = None

# Gauges are read on demand; history is written on this timer.
SAMPLE_SECONDS = 30.0
RETENTION_HOURS = 24

# How many processes the "heaviest right now" lists hold.
TOP_N = 15

# A resource is called out once when it crosses, and again when it recovers.
# The gap between the two stops a value hovering on the line from filling the
# log with alternating entries.
THRESHOLDS = {
    "cpu": (90.0, "CPU"),
    "memory": (90.0, "Memory"),
    "swap": (25.0, "Swap"),
    "disk": (90.0, "Disk"),
    "cpu_temp": (85.0, "CPU temperature"),
    "gpu_temp": (80.0, "GPU temperature"),
    "vram": (90.0, "VRAM"),
}
CLEAR_MARGIN = 10.0

NVIDIA_SMI = shutil.which("nvidia-smi")
GPU_TIMEOUT = 2.5


# --------------------------------------------------------------------- GPU


def _nvidia(args: list[str]) -> list[list[str]]:
    if not NVIDIA_SMI:
        return []
    try:
        result = subprocess.run(
            [NVIDIA_SMI, *args, "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=GPU_TIMEOUT,
            errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []
    rows = []
    for line in result.stdout.strip().splitlines():
        if line.strip():
            rows.append([cell.strip() for cell in line.split(",")])
    return rows


def _number(text: str) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def gpu() -> dict:
    """The NVIDIA card, if there is one. Empty dict when there is not."""
    rows = _nvidia(
        [
            "--query-gpu=name,utilization.gpu,memory.used,memory.total,"
            "temperature.gpu,power.draw,power.limit,clocks.sm,fan.speed"
        ]
    )
    if not rows:
        return {}

    cells = rows[0]
    while len(cells) < 9:
        cells.append("0")

    return {
        "name": cells[0],
        "percent": _number(cells[1]),
        # nvidia-smi reports MiB; everything else here is bytes.
        "vram_used": int(_number(cells[2]) * 1024 * 1024),
        "vram_total": int(_number(cells[3]) * 1024 * 1024),
        "temp": _number(cells[4]),
        "power": _number(cells[5]),
        "power_limit": _number(cells[6]),
        "clock_mhz": _number(cells[7]),
        "fan": _number(cells[8]),
    }


def gpu_processes() -> dict[int, int]:
    """VRAM per pid. This is what makes 'which app is using the card' answerable."""
    usage: dict[int, int] = {}
    for row in _nvidia(["--query-compute-apps=pid,used_memory"]):
        if len(row) < 2:
            continue
        try:
            pid = int(row[0])
        except ValueError:
            continue
        usage[pid] = int(_number(row[-1]) * 1024 * 1024)
    return usage


# ------------------------------------------------------------------ machine


def _cpu_temperature() -> float:
    """The package reading, which is the one that means 'the chip is hot'."""
    if psutil is None:
        return 0.0
    try:
        chips = psutil.sensors_temperatures()
    except Exception:
        return 0.0

    for name in ("coretemp", "k10temp", "zenpower", "acpitz"):
        for entry in chips.get(name, []):
            label = (entry.label or "").lower()
            if "package" in label or "tctl" in label or not label:
                return round(entry.current, 1)
    # Nothing labelled a package — take the hottest thing we can see.
    readings = [entry.current for entries in chips.values() for entry in entries]
    return round(max(readings), 1) if readings else 0.0


def snapshot() -> dict:
    """Everything the gauges need, read now."""
    if psutil is None:
        return {}

    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")

    try:
        frequency = psutil.cpu_freq()
        mhz = round(frequency.current, 0) if frequency else 0.0
    except Exception:
        mhz = 0.0

    try:
        load1, load5, load15 = psutil.getloadavg()
    except (OSError, AttributeError):
        load1 = load5 = load15 = 0.0

    card = gpu()

    return {
        "ts": utcnow(),
        "cpu_percent": psutil.cpu_percent(interval=None),
        "cpu_per_core": psutil.cpu_percent(interval=None, percpu=True),
        "cpu_count": psutil.cpu_count(logical=True) or 0,
        "cpu_physical": psutil.cpu_count(logical=False) or 0,
        "cpu_freq_mhz": mhz,
        "cpu_temp": _cpu_temperature(),
        "load1": round(load1, 2),
        "load5": round(load5, 2),
        "load15": round(load15, 2),
        "mem_used": memory.total - memory.available,
        "mem_total": memory.total,
        "mem_available": memory.available,
        "mem_cached": getattr(memory, "cached", 0),
        "mem_percent": round((memory.total - memory.available) * 100.0 / memory.total, 1),
        "swap_used": swap.used,
        "swap_total": swap.total,
        "swap_percent": round(swap.percent, 1),
        "disk_used": disk.used,
        "disk_total": disk.total,
        "disk_percent": round(disk.percent, 1),
        "uptime_seconds": round(time.time() - psutil.boot_time()),
        "gpu": card,
    }


def disks() -> list[dict]:
    """Every real mount point, so a full disk is visible before it bites."""
    if psutil is None:
        return []
    out = []
    seen: set[str] = set()
    for part in psutil.disk_partitions(all=False):
        if part.device in seen or part.fstype in ("squashfs", "tmpfs", "devtmpfs", "efivarfs"):
            continue
        seen.add(part.device)
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        out.append(
            {
                "device": part.device,
                "mount": part.mountpoint,
                "fstype": part.fstype,
                "used": usage.used,
                "total": usage.total,
                "percent": round(usage.percent, 1),
            }
        )
    return out


# ---------------------------------------------------------------- processes


def _managed_pids() -> dict[int, str]:
    """Every pid belonging to a registered app, including its children.

    An app is charged for what it spawned — `npm start` is not innocent of the
    node process doing the work — so the tree is walked rather than the single
    pid God Father happens to hold.
    """
    from .manager import manager

    owned: dict[int, str] = {}
    if psutil is None:
        return owned

    with db() as conn:
        names = {row["id"]: row["name"] for row in conn.execute("SELECT id, name FROM apps")}

    for app_id, name in names.items():
        process = manager.peek(app_id)
        if process is None or not process.is_alive or process.proc is None:
            continue
        try:
            parent = psutil.Process(process.proc.pid)
        except Exception:
            continue
        owned[parent.pid] = name
        try:
            for child in parent.children(recursive=True):
                owned[child.pid] = name
        except Exception:
            continue
    return owned


def processes(limit: int = TOP_N) -> dict:
    """The heaviest processes, plus every registered app whether busy or not."""
    if psutil is None:
        return {"top_cpu": [], "top_memory": [], "apps": [], "total": 0}

    owned = _managed_pids()
    vram = gpu_processes()

    rows: list[dict] = []
    per_app: dict[str, dict] = {}

    for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info", "username"]):
        info = proc.info
        pid = info.get("pid")
        if pid is None:
            continue
        memory = info.get("memory_info")
        rss = memory.rss if memory else 0
        # cpu_percent is measured since the previous call on the same object;
        # process_iter keeps those objects, so this is a real interval figure.
        cpu = info.get("cpu_percent") or 0.0
        app = owned.get(pid)

        row = {
            "pid": pid,
            "name": info.get("name") or "?",
            "cpu": round(cpu, 1),
            "memory": rss,
            "vram": vram.get(pid, 0),
            "user": info.get("username") or "",
            "app": app,
        }
        rows.append(row)

        if app:
            bucket = per_app.setdefault(
                app, {"app": app, "cpu": 0.0, "memory": 0, "vram": 0, "processes": 0}
            )
            bucket["cpu"] += cpu
            bucket["memory"] += rss
            bucket["vram"] += vram.get(pid, 0)
            bucket["processes"] += 1

    # VRAM held by a pid psutil could not see still belongs on the total.
    for pid, used in vram.items():
        if not any(row["pid"] == pid for row in rows):
            rows.append(
                {"pid": pid, "name": "(gone)", "cpu": 0.0, "memory": 0, "vram": used, "user": "", "app": None}
            )

    for bucket in per_app.values():
        bucket["cpu"] = round(bucket["cpu"], 1)

    return {
        "top_cpu": sorted(rows, key=lambda r: r["cpu"], reverse=True)[:limit],
        "top_memory": sorted(rows, key=lambda r: r["memory"], reverse=True)[:limit],
        "top_vram": [r for r in sorted(rows, key=lambda r: r["vram"], reverse=True) if r["vram"]][:limit],
        "apps": sorted(per_app.values(), key=lambda r: r["cpu"], reverse=True),
        "total": len(rows),
    }


# ------------------------------------------------------------------ storing


def _store(reading: dict, rates: dict) -> None:
    card = reading.get("gpu") or {}
    with db() as conn:
        conn.execute(
            """INSERT INTO system_samples
                   (ts, cpu_percent, cpu_freq_mhz, cpu_temp, load1,
                    mem_used, mem_total, swap_used, disk_used, disk_total,
                    disk_read_rate, disk_write_rate, net_sent_rate, net_recv_rate,
                    gpu_percent, gpu_temp, gpu_power, vram_used, vram_total)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                reading["ts"],
                reading["cpu_percent"],
                reading["cpu_freq_mhz"],
                reading["cpu_temp"],
                reading["load1"],
                reading["mem_used"],
                reading["mem_total"],
                reading["swap_used"],
                reading["disk_used"],
                reading["disk_total"],
                rates.get("disk_read", 0.0),
                rates.get("disk_write", 0.0),
                rates.get("net_sent", 0.0),
                rates.get("net_recv", 0.0),
                card.get("percent", 0.0),
                card.get("temp", 0.0),
                card.get("power", 0.0),
                card.get("vram_used", 0),
                card.get("vram_total", 0),
            ),
        )


def record_event(kind: str, level: str, message: str, value: float, threshold: float,
                 culprits: list[dict] | None = None) -> None:
    with db() as conn:
        conn.execute(
            """INSERT INTO system_events (ts, kind, level, message, value, threshold, culprits)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (utcnow(), kind, level, message, value, threshold, json.dumps(culprits or [])),
        )


def sweep() -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=RETENTION_HOURS)).isoformat(
        timespec="seconds"
    )
    with db() as conn:
        conn.execute("DELETE FROM system_samples WHERE ts < ?", (cutoff,))
        # Events are kept a week — they are rare, small, and the whole point is
        # being able to look back further than the charts go.
        week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
        conn.execute("DELETE FROM system_events WHERE ts < ?", (week,))


# ---------------------------------------------------------------- the loop


class Monitor:
    """Samples the machine on a timer and notices when something crosses a line."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._previous_io: dict = {}
        self._previous_at: float = 0.0
        self._breached: set[str] = set()
        self.last_run: str = ""

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running or psutil is None:
            return
        self._stop.clear()
        # Prime cpu_percent so the first stored sample is a real figure rather
        # than the 0.0 psutil returns when it has nothing to compare against.
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            pass
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._thread = None

    def _rates(self) -> dict:
        """Disk and network throughput, from counter deltas."""
        if psutil is None:
            return {}
        now = time.monotonic()
        try:
            disk_io = psutil.disk_io_counters()
            net_io = psutil.net_io_counters()
        except Exception:
            return {}

        current = {
            "disk_read": disk_io.read_bytes if disk_io else 0,
            "disk_write": disk_io.write_bytes if disk_io else 0,
            "net_sent": net_io.bytes_sent if net_io else 0,
            "net_recv": net_io.bytes_recv if net_io else 0,
        }

        rates = {}
        if self._previous_io and self._previous_at:
            gap = now - self._previous_at
            if gap > 0:
                for key, value in current.items():
                    delta = value - self._previous_io.get(key, value)
                    # A counter that went backwards means it was reset, not
                    # that the machine un-read a file.
                    rates[key] = round(max(delta, 0) / gap, 1)

        self._previous_io = current
        self._previous_at = now
        return rates

    def _check(self, reading: dict) -> None:
        card = reading.get("gpu") or {}
        vram_total = card.get("vram_total") or 0
        values = {
            "cpu": reading["cpu_percent"],
            "memory": reading["mem_percent"],
            "swap": reading["swap_percent"],
            "disk": reading["disk_percent"],
            "cpu_temp": reading["cpu_temp"],
            "gpu_temp": card.get("temp", 0.0),
            "vram": (card.get("vram_used", 0) * 100.0 / vram_total) if vram_total else 0.0,
        }

        for kind, (limit, label) in THRESHOLDS.items():
            value = values.get(kind) or 0.0
            if value >= limit and kind not in self._breached:
                self._breached.add(kind)
                record_event(
                    kind,
                    "warning",
                    f"{label} reached {value:.0f}{'°C' if 'temp' in kind else '%'}",
                    round(value, 1),
                    limit,
                    self._culprits(kind),
                )
            elif kind in self._breached and value <= limit - CLEAR_MARGIN:
                self._breached.discard(kind)
                record_event(
                    kind,
                    "cleared",
                    f"{label} back to {value:.0f}{'°C' if 'temp' in kind else '%'}",
                    round(value, 1),
                    limit,
                    None,
                )

    def _culprits(self, kind: str) -> list[dict]:
        """Who was holding the resource when it crossed the line."""
        try:
            found = processes(limit=5)
        except Exception:
            return []
        if kind == "vram":
            return found["top_vram"][:5]
        if kind in ("memory", "swap"):
            return found["top_memory"][:5]
        return found["top_cpu"][:5]

    def _loop(self) -> None:
        rounds = 0
        while not self._stop.is_set():
            try:
                reading = snapshot()
                if reading:
                    self._store_safe(reading, self._rates())
                    self._check(reading)
                    self.last_run = reading["ts"]
            except Exception:
                pass

            rounds += 1
            # Roughly every half hour at the default interval.
            if rounds >= 60:
                rounds = 0
                try:
                    sweep()
                except Exception:
                    pass

            self._stop.wait(SAMPLE_SECONDS)

    def _store_safe(self, reading: dict, rates: dict) -> None:
        try:
            _store(reading, rates)
        except Exception:
            pass


monitor = Monitor()


# ---------------------------------------------------------------- querying


def history(minutes: int = 60, points: int = 120) -> list[dict]:
    """Samples over the window, folded down to a drawable number of points."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(
        timespec="seconds"
    )
    with db() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM system_samples WHERE ts >= ? ORDER BY id", (since,)
            )
        ]
    if not rows:
        return []

    size = max(1, len(rows) // points + (1 if len(rows) % points else 0))
    folded = []
    for index in range(0, len(rows), size):
        chunk = rows[index : index + size]

        def mean(key: str) -> float:
            return round(sum(row[key] or 0 for row in chunk) / len(chunk), 1)

        folded.append(
            {
                "ts": chunk[-1]["ts"],
                "cpu": mean("cpu_percent"),
                "cpu_temp": mean("cpu_temp"),
                "memory": round(
                    sum((row["mem_used"] or 0) * 100.0 / (row["mem_total"] or 1) for row in chunk)
                    / len(chunk),
                    1,
                ),
                "swap": max(row["swap_used"] or 0 for row in chunk),
                "gpu": mean("gpu_percent"),
                "gpu_temp": mean("gpu_temp"),
                "vram": round(
                    sum(
                        (row["vram_used"] or 0) * 100.0 / (row["vram_total"] or 1)
                        for row in chunk
                    )
                    / len(chunk),
                    1,
                ),
                "mem_used": max(row["mem_used"] or 0 for row in chunk),
                "vram_used": max(row["vram_used"] or 0 for row in chunk),
                "disk_read": mean("disk_read_rate"),
                "disk_write": mean("disk_write_rate"),
                "net_sent": mean("net_sent_rate"),
                "net_recv": mean("net_recv_rate"),
            }
        )
    return folded


def events(limit: int = 100) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM system_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    out = []
    for row in rows:
        item = dict(row)
        try:
            item["culprits"] = json.loads(item["culprits"])
        except (ValueError, TypeError):
            item["culprits"] = []
        out.append(item)
    return out
