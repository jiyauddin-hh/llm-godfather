"""Requests, errors and load per application, for the Analytics tab."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from .. import traffic
from ..database import db
from ..models import TrafficApp, TrafficPath, TrafficPoint, TrafficState, TrafficTotals
from ..ports import one_scan
from ..service import runtime_for

router = APIRouter(prefix="/api/traffic", tags=["traffic"])

NOT_CAPTURED = (
    "Running outside God Father, so its output is not captured and there is "
    "nothing to count. Stop it where it runs and press Start here."
)
NEVER_STARTED = "Never started by God Father, so there is no log to read yet."
NO_ACCESS_LOG = (
    "Its log has no request lines. The server may not print an access log "
    "(Vite, uvicorn --no-access-log, or a non-HTTP service)."
)


def _note(counted: bool, status: str, has_log: bool) -> str:
    if counted:
        return ""
    if status == "external":
        return NOT_CAPTURED
    if not has_log:
        return NEVER_STARTED
    return NO_ACCESS_LOG


@router.get("", response_model=TrafficState)
def state(minutes: int = 60) -> TrafficState:
    minutes = max(5, min(minutes, traffic.RETENTION_DAYS * 1440))
    data = traffic.summary(minutes)
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")

    with db() as conn:
        rows = conn.execute("SELECT * FROM apps ORDER BY name COLLATE NOCASE").fetchall()
        events: dict[int, dict[str, int]] = {}
        for row in conn.execute(
            """SELECT app_id, type, COUNT(*) AS n FROM events
               WHERE created_at >= ? AND type IN ('crashed', 'restarted')
               GROUP BY app_id, type""",
            (since,),
        ):
            events.setdefault(row["app_id"], {})[row["type"]] = row["n"]

    grand = data["totals"]["requests"]
    totals = TrafficTotals(**data["totals"], apps_total=len(rows))
    apps: list[TrafficApp] = []

    with one_scan():
        for row in rows:
            runtime = runtime_for(row["id"], row["port"], bool(row["share_lan"]))
            figures = data["apps"].get(row["id"], {})
            last_minute = data["ever_counted"].get(row["id"])
            counted = last_minute is not None
            has_log = traffic.log_path(row["id"]).exists()
            crashes = events.get(row["id"], {}).get("crashed", 0)
            restarts = events.get(row["id"], {}).get("restarted", 0)

            apps.append(
                TrafficApp(
                    **{key: value for key, value in figures.items() if key != "spark" and key != "top_paths"},
                    app_id=row["id"],
                    name=row["name"],
                    port=row["port"],
                    status=runtime.status,
                    counted=counted,
                    note=_note(counted, runtime.status, has_log),
                    last_request=f"{last_minute}:00+00:00" if last_minute else None,
                    share=round(figures.get("requests", 0) * 100.0 / grand, 1) if grand else 0.0,
                    cpu_percent=runtime.cpu_percent,
                    memory_mb=runtime.memory_mb,
                    uptime_seconds=runtime.uptime_seconds,
                    crashes=crashes,
                    restarts=restarts,
                    spark=figures.get("spark", []),
                    top_paths=[TrafficPath(**item) for item in figures.get("top_paths", [])],
                )
            )

            totals.apps_counted += 1 if counted else 0
            totals.apps_active += 1 if figures.get("requests") else 0
            if runtime.status in {"running", "external", "starting"}:
                totals.running += 1
            elif runtime.status == "crashed":
                totals.crashed += 1
            else:
                totals.stopped += 1
            totals.cpu_percent += runtime.cpu_percent or 0.0
            totals.memory_mb += runtime.memory_mb or 0.0
            totals.crashes += crashes
            totals.restarts += restarts

    totals.cpu_percent = round(totals.cpu_percent, 1)
    totals.memory_mb = round(totals.memory_mb, 1)
    # Busiest first; the uncounted sink to the bottom rather than posing as idle.
    apps.sort(key=lambda app: (not app.counted, -app.requests, app.name.lower()))

    return TrafficState(
        monitoring=traffic.monitor.running,
        last_scan=traffic.monitor.last_run,
        last_error=traffic.monitor.last_error,
        sample_seconds=traffic.SAMPLE_SECONDS,
        retention_days=traffic.RETENTION_DAYS,
        window_minutes=minutes,
        bucket_minutes=data["bucket_minutes"],
        totals=totals,
        apps=apps,
        points=[TrafficPoint(**point) for point in data["series"]],
        by_hour=data["by_hour"],
        top_paths=[TrafficPath(**item) for item in data["top_paths"]],
    )


@router.post("/scan", response_model=TrafficState)
def scan_now(minutes: int = 60) -> TrafficState:
    """Read new log lines immediately instead of waiting for the timer."""
    traffic.scan_all()
    return state(minutes)
