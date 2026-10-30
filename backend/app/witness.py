"""File a photograph against a lifecycle event — and against a power cut.

A log line says an application stopped at 03:14. It does not say who was sitting
there. This module closes that gap, and it does it by *looking a frame up*
rather than taking one.

`ring.py` keeps the camera open and writes a frame every few seconds. So when an
app crashes there is no shutter to open: `nearest()` finds the frame closest to
the moment and it is copied into `captures/` as a permanent record. That is
instant — the old design queued the event and spent ~2s on sensor warm-up before
it could shoot — and it can file a frame from *before* the event, which an
event-triggered camera can never do.

Three things follow from working this way, and they replace three pieces of
machinery this module used to need:

*No worker thread.* Copying a file that already exists costs nothing, so the
work happens inline on the hot path without slowing anything down.

*No cooldown.* A restart fires `stopped` and `started` two seconds apart; both
resolve to the same ring frame, and because the capture is named after that
frame they land on one file on disk without any special case. Deleting one still
leaves the other, exactly as before.

*Nothing to do when the power goes.* Frames are already on disk the moment they
are taken. `recover()` runs on the *next* boot, notices the last run never
recorded a clean exit, and promotes the newest surviving frame to a `power_loss`
capture — timestamped with the frame's own mtime, so the log says when the
machine died and not merely that it did.
"""

import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from . import camera, ring
from .config import CAPTURE_DIR, INCIDENT_DIR, capture_path, incident_path
from .database import db, get_setting, set_setting, utcnow

SETTING_ENABLED = "camera_enabled"
SETTING_DEVICE = "camera_device"
SETTING_MAX_MB = "camera_max_mb"
SETTING_STARTS = "camera_capture_starts"
SETTING_INCIDENT_MINUTES = "camera_incident_minutes"

# Written at startup, blanked on a clean exit. Found non-empty on boot, it means
# the last run ended without ever getting to its shutdown path — a power cut, a
# SIGKILL, a hard reset. It is the whole of the detection; the ring supplies the
# time and the picture.
SETTING_RUNNING = "camera_running_since"

DEFAULT_MAX_MB = 500

# How much of the run-up to a power cut to rescue. Every other event keeps its
# surroundings for free — they stay in the ring — but a power cut's lead-up is
# swept within seconds of the next boot unless it is copied out first. Five
# minutes is ~30 frames, under a megabyte.
DEFAULT_INCIDENT_MINUTES = 5

# Absent-vs-empty needs distinguishing when deciding whether anyone has ever
# made a choice about the camera, and get_setting collapses the two.
_ABSENT = "\x00absent"

# Events worth a photograph.
#
# `restarted` is absent because it is always bracketed by the `stopped` and
# `started` that already resolve to a frame. `started` is present but off by
# default — see capture_starts(). `power_loss` is never passed to record(); it
# is synthesised by recover() on the next boot.
KINDS = {"started", "stopped", "crashed", "takeover", "shutdown", "power_loss", "test"}

# Kinds a caller may actually record.
RECORDABLE = {"started", "stopped", "crashed", "takeover", "shutdown"}


def enabled() -> bool:
    return get_setting(SETTING_ENABLED, "0") == "1"


def device() -> str:
    return get_setting(SETTING_DEVICE, "").strip() or (camera.default_device() or "")


def capture_starts() -> bool:
    """Whether `started` is worth a photograph. Off by default.

    Autostart deliberately spaces launches out, so a boot produces one `started`
    per application and every one of them photographs an empty room. The crash
    that an auto-restart follows already has its own frame from moments earlier,
    so almost nothing is lost by leaving these out — and the log stays readable.
    """
    return get_setting(SETTING_STARTS, "0") == "1"


def incident_minutes() -> float:
    try:
        return max(0.5, min(120.0, float(get_setting(SETTING_INCIDENT_MINUTES,
                                                     str(DEFAULT_INCIDENT_MINUTES)))))
    except ValueError:
        return float(DEFAULT_INCIDENT_MINUTES)


def max_bytes() -> int:
    try:
        return max(1, int(get_setting(SETTING_MAX_MB, str(DEFAULT_MAX_MB)))) * 1024 * 1024
    except ValueError:
        return DEFAULT_MAX_MB * 1024 * 1024


def configure(
    on: bool | None = None,
    dev: str | None = None,
    max_mb: int | None = None,
    starts: bool | None = None,
    incident_min: float | None = None,
) -> None:
    if on is not None:
        set_setting(SETTING_ENABLED, "1" if on else "0")
    if dev is not None:
        set_setting(SETTING_DEVICE, dev.strip())
    if max_mb is not None:
        set_setting(SETTING_MAX_MB, str(max(1, int(max_mb))))
    if starts is not None:
        set_setting(SETTING_STARTS, "1" if starts else "0")
    if incident_min is not None:
        set_setting(SETTING_INCIDENT_MINUTES, str(max(0.5, min(120.0, float(incident_min)))))


def seed_defaults() -> None:
    """On a machine that has never been asked, switch recording on — if it can.

    Seeded as a real row rather than left to fall through to a default, so the
    stored state always says what is happening and a later change of mind about
    the default cannot silently re-flip every existing install.

    Gated on a camera actually being present, which matters more than it looks:
    without the gate a headless box would file a row for every lifecycle event
    with 'no camera found' where the photo should be, and the log fills with
    evidence of nothing. The guard on the settings endpoint cannot catch that,
    because a default never goes through the endpoint.
    """
    if get_setting(SETTING_ENABLED, _ABSENT) != _ABSENT:
        return  # already decided — by a person, or by an earlier boot
    if camera.unavailable_reason() is not None:
        return
    set_setting(SETTING_ENABLED, "1")
    if not get_setting(SETTING_DEVICE, "").strip():
        set_setting(SETTING_DEVICE, camera.default_device() or "")


# ------------------------------------------------------------------ storage


def _insert(
    app_id: int | None,
    app_name: str,
    kind: str,
    message: str,
    file: str,
    ok: bool,
    note: str,
    created_at: str | None = None,
    clip: str = "",
) -> int | None:
    try:
        with db() as conn:
            cursor = conn.execute(
                """INSERT INTO captures
                       (app_id, app_name, kind, message, file, clip, ok, note, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (app_id, app_name, kind, message, file, clip, int(ok), note,
                 created_at or utcnow()),
            )
            return cursor.lastrowid
    except Exception:
        return None


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def _keep(frame: Path) -> str:
    """Copy a ring frame into the permanent folder and return its stored name.

    Named after the frame it came from, which quietly does the job the old
    four-second cooldown used to: two events that resolve to the same frame
    resolve to the same filename, so they share one file on disk with no
    special case, and the delete path already knows not to remove a file another
    row still points at.
    """
    name = f"capture-{frame.name[len('frame-'):]}"
    destination = capture_path(name)
    if not destination.exists():
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(frame, destination)
    return name


def _why_no_frame() -> str:
    """What to write where the photo should have been."""
    if not enabled():
        return "the camera log was off"
    reason = camera.unavailable_reason()
    if reason:
        return reason
    if ring.ring.error():
        return ring.ring.error()
    if not ring.ring.running():
        return "the recording was not running"
    return "the recording had not taken its first frame yet"


def _file_event(
    app_id: int | None,
    app_name: str,
    kind: str,
    message: str,
    when: float,
    created_at: str | None = None,
    clip: str = "",
) -> int | None:
    """Look up the frame nearest `when` and file it against the event."""
    found = ring.nearest(when)
    if found is None:
        return _insert(app_id, app_name, kind, message, "", False, _why_no_frame(),
                       created_at, clip)

    frame, gap = found
    try:
        name = _keep(frame)
    except OSError as exc:
        return _insert(app_id, app_name, kind, message, "", False, str(exc), created_at, clip)

    # Worth saying only when the gap is bigger than the gap between frames —
    # below that it is just how a recording works.
    note = f"nearest frame is {gap:.0f}s from the event" if gap > ring.interval() else ""
    capture_id = _insert(app_id, app_name, kind, message, name, True, note, created_at, clip)
    prune()
    return capture_id


def reconcile() -> None:
    """Close off rows left mid-flight by the old queue-and-shoot capture path."""
    try:
        with db() as conn:
            conn.execute(
                "UPDATE captures SET note = ? WHERE ok = 0 AND file = '' AND note = ?",
                ("God Father stopped before the camera ran", "waiting for the camera"),
            )
    except Exception:
        pass


# --------------------------------------------------------- power-cut recovery


def mark_running() -> None:
    set_setting(SETTING_RUNNING, utcnow())


def mark_clean_exit() -> None:
    set_setting(SETTING_RUNNING, "")


def recover() -> int | None:
    """If the last run died rather than exited, file the last thing it saw.

    Must run before the ring's sweeper starts. After a long outage every
    surviving frame is older than the retention window, so the sweep would
    delete the evidence within seconds of boot — and it is the newest of those
    frames, the one taken as the lights went out, that this whole design exists
    to keep.
    """
    stamp = get_setting(SETTING_RUNNING, "")
    if not stamp:
        return None  # last run exited cleanly, or this is the first run ever
    mark_clean_exit()  # consume it, so a second boot does not report it twice

    latest = ring.newest()
    if latest is None:
        if not enabled():
            return None  # recording was off — there is nothing to say
        return _insert(
            None, "God Father", "power_loss",
            f"God Father was running since {stamp} and stopped without shutting down",
            "", False, "no frame of the recording survived", None,
        )

    taken, frame = latest

    # Rescue the run-up, not just the final frame. Every other kind of event
    # keeps its surroundings for free — they stay in the ring, and the live clip
    # can play them back for as long as they are held. A power cut cannot: the
    # sweeper runs moments from now and every one of these frames is already
    # older than the retention window, so without copying them out first the
    # only footage of what led to the cut is deleted seconds after the machine
    # comes back. Measured from the last frame rather than from now, because the
    # outage may have lasted days.
    window = incident_minutes()
    name = f"powercut-{time.strftime('%Y%m%d-%H%M%S', time.localtime(taken))}"
    kept = ring.preserve(ring.tail(window), incident_path(name))
    clip = name if kept >= 2 else ""

    lead_in = f" · {kept} frames of the {window:g} minutes before it" if clip else ""
    return _file_event(
        None, "God Father", "power_loss",
        "the machine lost power or was reset — this is the last frame before it went" + lead_in,
        taken,
        created_at=_iso(taken),
        clip=clip,
    )


# ------------------------------------------------------------------ the log


def take_test_shot() -> int | None:
    """Photograph now, ignoring the on/off switch.

    This is the button pressed *because* someone is unsure the camera works, so
    the switch must not be able to make it do nothing. Which of the two paths it
    takes is itself the useful answer: if the recording is running it files the
    newest frame, proving the recording is live; if it is not, the device is
    free and one is taken the old way.
    """
    if ring.ring.running():
        latest = ring.newest()
        if latest is not None:
            taken, frame = latest
            age = max(0.0, ring.ring.newest_age() or 0.0)
            return _file_event(
                None, "Test shot", "test",
                f"from the recording, {age:.0f}s old",
                taken,
            )

    capture_id = _insert(None, "Test shot", "test", "taken directly, the recording is not running",
                         "", False, "waiting for the camera", None)
    if capture_id is None:
        return None
    name = f"capture-test-{capture_id:06d}.jpg"
    ok, note = camera.capture(capture_path(name), device())
    try:
        with db() as conn:
            conn.execute(
                "UPDATE captures SET file = ?, ok = ?, note = ? WHERE id = ?",
                (name if ok else "", int(ok), note, capture_id),
            )
    except Exception:
        pass
    if ok:
        prune()
    return capture_id


def prune() -> int:
    """Drop the oldest captures until the folder fits under the cap.

    Files are shared between rows that resolved to the same frame, so a file is
    only removed once nothing still points at it.
    """
    ceiling = max_bytes()
    try:
        with db() as conn:
            rows = conn.execute("SELECT id, file FROM captures ORDER BY id DESC").fetchall()
    except Exception:
        return 0

    counted: set[str] = set()
    total = 0
    doomed: list[int] = []

    for row in rows:
        if doomed:
            doomed.append(row["id"])
            continue
        name = row["file"]
        if name and name not in counted:
            counted.add(name)
            try:
                total += capture_path(name).stat().st_size
            except OSError:
                pass
        if total > ceiling:
            doomed.append(row["id"])

    if not doomed:
        sweep_orphans()
        return 0

    try:
        with db() as conn:
            placeholders = ",".join("?" * len(doomed))
            gone = conn.execute(
                f"SELECT DISTINCT file FROM captures WHERE id IN ({placeholders}) AND file != ''",
                doomed,
            ).fetchall()
            conn.execute(f"DELETE FROM captures WHERE id IN ({placeholders})", doomed)
            still_used = {
                r["file"]
                for r in conn.execute("SELECT DISTINCT file FROM captures WHERE file != ''")
            }
    except Exception:
        return 0

    for row in gone:
        if row["file"] not in still_used:
            capture_path(row["file"]).unlink(missing_ok=True)

    sweep_orphans()
    return len(doomed)


def incident_bytes() -> int:
    try:
        return sum(p.stat().st_size for p in INCIDENT_DIR.rglob("*.jpg"))
    except OSError:
        return 0


def drop_incident(name: str) -> None:
    """Remove a preserved folder, once nothing points at it any more."""
    if not name:
        return
    try:
        shutil.rmtree(incident_path(name), ignore_errors=True)
    except OSError:
        pass


def sweep_incident_orphans() -> None:
    """Delete preserved folders whose capture row has gone."""
    try:
        with db() as conn:
            known = {
                r["clip"] for r in conn.execute("SELECT DISTINCT clip FROM captures WHERE clip != ''")
            }
    except Exception:
        return
    try:
        for folder in INCIDENT_DIR.iterdir():
            if folder.is_dir() and folder.name not in known:
                shutil.rmtree(folder, ignore_errors=True)
    except OSError:
        pass


def sweep_orphans() -> None:
    """Delete image files no row refers to — a crash mid-write leaves these."""
    try:
        with db() as conn:
            known = {
                r["file"] for r in conn.execute("SELECT DISTINCT file FROM captures WHERE file != ''")
            }
    except Exception:
        return
    try:
        for path in CAPTURE_DIR.glob("*.jpg"):
            if path.name not in known:
                path.unlink(missing_ok=True)
    except OSError:
        pass
    sweep_incident_orphans()


def folder_bytes() -> int:
    try:
        return sum(p.stat().st_size for p in CAPTURE_DIR.glob("*.jpg"))
    except OSError:
        return 0


# ------------------------------------------------------------- the recorder


class Witness:
    """Files events against the recording, and owns the recording's lifecycle."""

    def start(self) -> None:
        """Begin recording if the camera log is on and the hardware allows it."""
        if not enabled() or camera.unavailable_reason() is not None:
            ring.ring.stop()
            return
        ring.ring.start(device())

    def stop(self) -> None:
        ring.ring.stop()

    def apply_settings(self, restart_stream: bool = False) -> None:
        """Re-read the settings and bring the recording into line with them."""
        if not enabled() or camera.unavailable_reason() is not None:
            ring.ring.stop()
            return
        # Only bounce a stream that was already up: one started just now already
        # read the new settings, and restarting it would cost a warm-up for
        # nothing.
        was_running = ring.ring.running()
        ring.ring.start(device())
        if restart_stream and was_running:
            ring.ring.restart()

    def record(self, app_id: int | None, app_name: str, kind: str, message: str = "") -> None:
        """Note an event and file the nearest frame against it. Never raises."""
        if kind not in RECORDABLE or not enabled():
            return
        if kind == "started" and not capture_starts():
            return
        try:
            _file_event(app_id, app_name, kind, message, time.time())
        except Exception:
            pass


witness = Witness()
