"""The camera log: frames filed against what happened to this machine."""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response

from .. import camera, ring, witness
from ..config import capture_path, incident_path
from ..database import db
from ..models import CameraState, CameraUpdate, Capture, RingSummary

router = APIRouter(prefix="/api/captures", tags=["captures"])


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def _last_cut() -> tuple[int | None, str | None, int]:
    """The newest power cut that still has playable footage on disk."""
    try:
        with db() as conn:
            rows = conn.execute(
                """SELECT id, created_at, clip FROM captures
                   WHERE clip != '' ORDER BY id DESC LIMIT 5"""
            ).fetchall()
    except Exception:
        return None, None, 0
    for row in rows:
        held = ring.frames_in(incident_path(row["clip"]))
        if len(held) >= 2:
            return row["id"], row["created_at"], len(held)
    return None, None, 0


def _state() -> CameraState:
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM captures").fetchone()["n"]
    reason = camera.unavailable_reason()
    cut_id, cut_at, cut_frames = _last_cut()
    recorder = ring.ring
    age = recorder.newest_age()
    return CameraState(
        enabled=witness.enabled(),
        device=witness.device(),
        devices=camera.devices(),
        max_mb=witness.max_bytes() // (1024 * 1024),
        used_mb=round(witness.folder_bytes() / (1024 * 1024), 2),
        count=count,
        available=reason is None,
        unavailable_reason=reason,
        capture_starts=witness.capture_starts(),
        incident_minutes=witness.incident_minutes(),
        incident_mb=round(witness.incident_bytes() / (1024 * 1024), 2),
        last_cut_id=cut_id,
        last_cut_at=cut_at,
        last_cut_frames=cut_frames,
        recording=recorder.running(),
        healthy=recorder.healthy(),
        newest_age=None if age is None else round(age, 1),
        interval=ring.interval(),
        keep_minutes=ring.minutes(),
        ring_frames=len(ring.frames()),
        ring_mb=round(ring.folder_bytes() / (1024 * 1024), 2),
        restarts=recorder.restarts(),
        stream_error=recorder.error(),
    )


# Declared before /{capture_id} so these names are not read as ids.
@router.get("/settings", response_model=CameraState)
def settings() -> CameraState:
    return _state()


@router.post("/settings", response_model=CameraState)
def update_settings(payload: CameraUpdate) -> CameraState:
    if payload.enabled and camera.unavailable_reason():
        raise HTTPException(status_code=400, detail=camera.unavailable_reason())

    witness.configure(
        on=payload.enabled,
        dev=payload.device,
        max_mb=payload.max_mb,
        starts=payload.capture_starts,
        incident_min=payload.incident_minutes,
    )
    # The interval is baked into the running ffmpeg's filter graph, so changing
    # it means bouncing the stream; the retention window is only read by the
    # sweeper and takes effect on its own.
    bounce = ring.configure(seconds=payload.interval, keep_minutes=payload.keep_minutes)
    if payload.device is not None:
        bounce = True

    witness.witness.apply_settings(restart_stream=bounce)

    if payload.max_mb is not None:
        witness.prune()
    if payload.keep_minutes is not None:
        ring.sweep()
    return _state()


@router.post("/test", response_model=Capture)
def test_shot() -> Capture:
    """Prove the camera now, rather than discovering it at the worst moment."""
    reason = camera.unavailable_reason()
    if reason:
        raise HTTPException(status_code=400, detail=reason)

    capture_id = witness.take_test_shot()
    if capture_id is None:
        raise HTTPException(status_code=500, detail="could not record the capture")
    return _fetch(capture_id)


@router.get("/ring", response_model=RingSummary)
def ring_summary() -> RingSummary:
    """What the recording holds right now — how far back the clip can go."""
    held = ring.frames()
    gap = ring.interval()
    first = held[0][0] if held else None
    last = held[-1][0] if held else None
    minutes_held = round(((last - first) / 60) if held and len(held) > 1 else 0.0, 1)
    return RingSummary(
        frames=len(held),
        minutes_held=minutes_held,
        first=_iso(first) if first else None,
        last=_iso(last) if last else None,
        interval=gap,
        keep_minutes=ring.minutes(),
        fps=ring.CLIP_FPS,
        speed=gap * ring.CLIP_FPS,
        playable=len(held) >= 2,
        note="" if len(held) >= 2 else "not enough recorded yet to play",
    )


@router.get("/ring/clip")
def ring_clip(minutes: float = Query(5, gt=0, le=1440)) -> Response:
    """The last `minutes` of recording as an MP4, with the clock burnt in.

    Rendered on the spot rather than cached: the newest frame changes every few
    seconds, so anything kept would be stale before it was asked for twice.
    """
    data, note = ring.render_clip(minutes)
    if data is None:
        raise HTTPException(status_code=400, detail=note or "the clip could not be rendered")

    stamp = (ring.newest() or (0.0, None))[0]
    name = f"godfather-last-{minutes:g}min-{int(stamp)}.mp4"
    return Response(
        content=data,
        media_type="video/mp4",
        headers={
            "Content-Disposition": f'inline; filename="{name}"',
            # It is a different video every few seconds; never let it be reused.
            "Cache-Control": "no-store",
        },
    )


@router.get("", response_model=list[Capture])
def list_captures(limit: int = 200, offset: int = 0) -> list[Capture]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM captures ORDER BY id DESC LIMIT ? OFFSET ?",
            (max(1, min(limit, 1000)), max(0, offset)),
        ).fetchall()
    return [Capture(**{**dict(row), "ok": bool(row["ok"])}) for row in rows]


@router.delete("", status_code=204)
def clear_captures() -> None:
    """Empty the log. The recording keeps rolling — only the kept frames go."""
    with db() as conn:
        conn.execute("DELETE FROM captures")
    witness.sweep_orphans()


def _fetch(capture_id: int) -> Capture:
    with db() as conn:
        row = conn.execute("SELECT * FROM captures WHERE id = ?", (capture_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"capture {capture_id} not found")
    return Capture(**{**dict(row), "ok": bool(row["ok"])})


@router.get("/{capture_id}/image")
def capture_image(capture_id: int) -> FileResponse:
    record = _fetch(capture_id)
    if not record.file:
        raise HTTPException(status_code=404, detail=record.note or "no image was captured")
    path = capture_path(record.file)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="the image file is no longer on disk")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/{capture_id}/clip")
def capture_clip(capture_id: int) -> Response:
    """The preserved footage leading up to this capture, as an MP4.

    Only a power cut has any: every other event's surroundings are still in the
    ring, where the live clip can reach them for as long as they are held.
    """
    record = _fetch(capture_id)
    if not record.clip:
        raise HTTPException(
            status_code=404,
            detail="this capture has no preserved footage — for a recent event, play the recording instead",
        )

    folder = incident_path(record.clip)
    data, note = ring.render(ring.frames_in(folder))
    if data is None:
        raise HTTPException(status_code=404, detail=note or "the footage is no longer on disk")

    return Response(
        content=data,
        media_type="video/mp4",
        headers={
            "Content-Disposition": f'inline; filename="godfather-{record.clip}.mp4"',
            "Cache-Control": "no-store",
        },
    )


@router.delete("/{capture_id}", status_code=204)
def delete_capture(capture_id: int) -> None:
    record = _fetch(capture_id)
    with db() as conn:
        conn.execute("DELETE FROM captures WHERE id = ?", (capture_id,))
        # The file may be shared with a neighbouring event that reused the shot.
        shared = conn.execute(
            "SELECT 1 FROM captures WHERE file = ? AND file != '' LIMIT 1", (record.file,)
        ).fetchone()
        still_clipped = conn.execute(
            "SELECT 1 FROM captures WHERE clip = ? AND clip != '' LIMIT 1", (record.clip,)
        ).fetchone()
    if record.file and shared is None:
        capture_path(record.file).unlink(missing_ok=True)
    if record.clip and still_clipped is None:
        witness.drop_incident(record.clip)
