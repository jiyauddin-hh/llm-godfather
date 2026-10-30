"""What the machine is doing, and which application is responsible."""

from fastapi import APIRouter

from .. import system_monitor as sysmon
from ..models import (
    AppLoad,
    DiskRow,
    GpuInfo,
    ProcessRow,
    SystemEvent,
    SystemNow,
    SystemPoint,
    SystemState,
)

router = APIRouter(prefix="/api/system", tags=["system"])

NO_PSUTIL = (
    "psutil is not installed, so this machine cannot be measured. Install it with:  "
    "pip install psutil"
)

NO_GPU = (
    "No NVIDIA card found. CPU, memory and disk are still measured; GPU and VRAM "
    "need nvidia-smi, which comes with the NVIDIA driver."
)


@router.get("", response_model=SystemState)
def state(minutes: int = 60, top: int = sysmon.TOP_N) -> SystemState:
    if sysmon.psutil is None:
        return SystemState(available=False, unavailable_reason=NO_PSUTIL)

    minutes = max(5, min(minutes, 1440))
    top = max(3, min(top, 50))

    reading = sysmon.snapshot()
    found = sysmon.processes(limit=top)
    card = reading.get("gpu") or {}

    return SystemState(
        available=True,
        monitoring=sysmon.monitor.running,
        last_sample=sysmon.monitor.last_run,
        sample_seconds=sysmon.SAMPLE_SECONDS,
        retention_hours=sysmon.RETENTION_HOURS,
        window_minutes=minutes,
        now=SystemNow(**{**reading, "gpu": GpuInfo(**card) if card else None}),
        disks=[DiskRow(**row) for row in sysmon.disks()],
        top_cpu=[ProcessRow(**row) for row in found["top_cpu"]],
        top_memory=[ProcessRow(**row) for row in found["top_memory"]],
        top_vram=[ProcessRow(**row) for row in found["top_vram"]],
        apps=[AppLoad(**row) for row in found["apps"]],
        process_count=found["total"],
        points=[SystemPoint(**point) for point in sysmon.history(minutes)],
        events=[SystemEvent(**event) for event in sysmon.events(60)],
        gpu_note="" if card else NO_GPU,
    )


@router.get("/now", response_model=SystemNow)
def now() -> SystemNow:
    """Just the gauges, for a caller that does not want the history."""
    reading = sysmon.snapshot()
    if not reading:
        return SystemNow()
    card = reading.get("gpu") or {}
    return SystemNow(**{**reading, "gpu": GpuInfo(**card) if card else None})


@router.delete("/events", status_code=204)
def clear_events() -> None:
    from ..database import db

    with db() as conn:
        conn.execute("DELETE FROM system_events")
