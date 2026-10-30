"""The rolling recording — a dashcam for the machine.

A power cut gives no notice. Nothing is signalled, no handler runs, the lights
simply go out mid-instruction. So the one moment most worth a photograph — the
room as the machine died — is the one moment a camera triggered *by* events can
never catch.

The fix is to stop taking photographs and start reading a tape. ffmpeg holds the
camera open for God Father's whole life and drops one JPEG every few seconds
into `ring/`. A sweeper deletes anything older than the retention window. Two
things follow from that, and both are the point:

*A capture becomes a lookup.* When an app crashes there is no shutter to open —
the frame already exists, and `nearest()` picks the one closest to the moment.
That is instant where the old path cost ~2s of sensor warm-up, and it can return
a frame from *before* the event rather than two seconds after it.

*The power cut needs no handler.* Every frame is already on disk the instant it
is taken, so there is nothing to save when the power goes. The newest surviving
file simply *is* the last thing the machine saw. Recovering it is a job for the
next boot, not for the microsecond the power drops — see `witness.recover()`.

Two design choices are load-bearing:

*No database row per frame.* 8,640 throwaway inserts a day is the small reason.
The real one is that a frame written during a power cut should be a file write
and nothing more; a transaction is the thing you least want in flight when the
disk stops. The filename carries the time, the filesystem carries the ordering,
and there is no index that can be left corrupt.

*The stream is supervised.* A long-lived ffmpeg will eventually die — a USB
reset, a yanked cable — and the failure is silent: God Father keeps running, the
UI keeps looking healthy, and the recording has stopped. You would find out
weeks later, at exactly the wrong moment. So the process is watched and
restarted with backoff, and how old the newest frame is gets published to the UI
as a first-class fact rather than assumed. It is also reaped: a SIGKILLed God
Father cannot take its ffmpeg with it, and an orphan left holding /dev/video0
would stop the *next* God Father from ever recording — the feature failing in
precisely the case it was built for.
"""

import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import camera
from .config import LOG_DIR, RING_DIR
from .database import get_setting, set_setting

SETTING_INTERVAL = "camera_ring_interval"
SETTING_MINUTES = "camera_ring_minutes"

# One frame per this many seconds. With the sensor already warm a frame is
# nearly free, so this is chosen for how finely you want to place an event in
# time, not for cost. Below ~5s you are mostly churning files.
DEFAULT_INTERVAL = 10
MIN_INTERVAL, MAX_INTERVAL = 2, 300

# How much of the recent past to keep. At ~30 KB a frame and one frame per ten
# seconds this is about 3.6 MB — storage is not the constraint here, so the
# window is chosen for how much context is useful, and for how much continuous
# recording of a room is reasonable to hold. Twenty minutes is both.
DEFAULT_MINUTES = 20
MIN_MINUTES, MAX_MINUTES = 1, 1440

# The sweep runs far more often than the window is long, so the folder never
# overshoots by more than a few frames.
SWEEP_SECONDS = 15.0

# ffmpeg gets this long to shut down before it is killed. It only has to flush
# one small JPEG.
STOP_GRACE = 4.0

# Restart backoff after the stream dies, in seconds. A camera that has been
# unplugged should not be retried in a hot loop, but one that hiccuped should
# come back quickly.
BACKOFF = (2, 4, 8, 15, 30, 60)

# Playback rate of a rendered clip. One recorded frame becomes exactly one
# frame of video, so at the default interval a second of clip is forty seconds
# of real time.
CLIP_FPS = 4

# A frame is held for the real gap until the next one — that is what makes the
# clip honest about pacing — but never for longer than this many intervals. An
# outage would otherwise hold one stale image for minutes while the burnt-in
# clock ran on over it, which reads as "the room did not change" when the truth
# is "we have no idea". Clamped, the gap shows up as a jump in the clock
# instead, which is what actually happened.
CLIP_MAX_HOLD = 2

# Long enough for a full window on a slow machine, short enough that a wedged
# encode cannot hold a request open.
CLIP_TIMEOUT = 90.0

# ffmpeg's own complaints, truncated on every spawn so it cannot grow.
STREAM_LOG = LOG_DIR / "camera-stream.log"

# strftime, so every frame names the wall-clock second it was taken and a
# directory listing is already a timeline.
PATTERN = "frame-%Y%m%d-%H%M%S.jpg"
GLOB = "frame-*.jpg"


def interval() -> int:
    try:
        value = int(get_setting(SETTING_INTERVAL, str(DEFAULT_INTERVAL)))
    except ValueError:
        return DEFAULT_INTERVAL
    return max(MIN_INTERVAL, min(MAX_INTERVAL, value))


def minutes() -> int:
    try:
        value = int(get_setting(SETTING_MINUTES, str(DEFAULT_MINUTES)))
    except ValueError:
        return DEFAULT_MINUTES
    return max(MIN_MINUTES, min(MAX_MINUTES, value))


def configure(seconds: int | None = None, keep_minutes: int | None = None) -> bool:
    """Store new settings. Returns True if the stream needs restarting to apply."""
    changed = False
    if seconds is not None:
        value = max(MIN_INTERVAL, min(MAX_INTERVAL, int(seconds)))
        # Stored even when it already matches what is in effect. The return
        # value says whether the *stream* needs bouncing; whether the setting is
        # written is a separate question, and leaving it to fall through to a
        # default would mean a later change of default silently re-tuning a
        # machine somebody had already configured.
        changed = value != interval()
        set_setting(SETTING_INTERVAL, str(value))
    if keep_minutes is not None:
        value = max(MIN_MINUTES, min(MAX_MINUTES, int(keep_minutes)))
        set_setting(SETTING_MINUTES, str(value))
    return changed


# ------------------------------------------------------------------- frames


def frames() -> list[tuple[float, Path]]:
    """Every ring frame as (taken_at_epoch, path), oldest first.

    Modification time rather than the name: it is the moment the frame landed,
    it needs no parsing, and it carries no timezone to get wrong. The strftime
    name is for whoever opens the folder.
    """
    found: list[tuple[float, Path]] = []
    try:
        for path in RING_DIR.glob(GLOB):
            try:
                found.append((path.stat().st_mtime, path))
            except OSError:
                continue
    except OSError:
        return []
    found.sort()
    return found


def newest() -> tuple[float, Path] | None:
    found = frames()
    return found[-1] if found else None


def nearest(when: float) -> tuple[Path, float] | None:
    """The frame closest in time to `when`, as (path, seconds_away).

    Closest in either direction on purpose. The frame taken four seconds
    *before* an app crashed is a better record of who was sitting there than one
    taken six seconds after, and the old event-triggered path could only ever
    look forwards.
    """
    found = frames()
    if not found:
        return None
    taken, path = min(found, key=lambda item: abs(item[0] - when))
    return path, abs(taken - when)


def folder_bytes() -> int:
    try:
        return sum(path.stat().st_size for _, path in frames())
    except OSError:
        return 0


def recent(window_minutes: float) -> list[tuple[float, Path]]:
    """Frames from the last `window_minutes`, oldest first."""
    cutoff = time.time() - window_minutes * 60
    return [item for item in frames() if item[0] >= cutoff]


def span(window_minutes: float) -> tuple[float, float] | None:
    """(first, last) capture time within the window, or None if it is empty."""
    found = recent(window_minutes)
    return (found[0][0], found[-1][0]) if found else None


def tail(window_minutes: float) -> list[tuple[float, Path]]:
    """The last `window_minutes` of recording, measured back from the *newest
    frame* rather than from now.

    The distinction is the whole point when recovering a power cut: the machine
    may have been down for days, so every surviving frame is ancient and a
    window measured from the present moment would come back empty — throwing
    away the footage at the exact moment it is being rescued.
    """
    found = frames()
    if not found:
        return []
    cutoff = found[-1][0] - window_minutes * 60
    return [item for item in found if item[0] >= cutoff]


def frames_in(folder: Path) -> list[tuple[float, Path]]:
    """Frames of a preserved incident folder, oldest first."""
    found: list[tuple[float, Path]] = []
    try:
        for path in folder.glob(GLOB):
            try:
                found.append((path.stat().st_mtime, path))
            except OSError:
                continue
    except OSError:
        return []
    found.sort()
    return found


def preserve(found: list[tuple[float, Path]], destination: Path) -> int:
    """Copy frames somewhere the sweeper will not reach. Returns how many landed.

    copy2 rather than copy, because the modification time *is* the capture time
    here — it is what dates the frames and what orders them once they are out of
    the ring.
    """
    if not found:
        return 0
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0
    kept = 0
    for _, path in found:
        try:
            shutil.copy2(path, destination / path.name)
            kept += 1
        except OSError:
            continue
    return kept


def _spacing(found: list[tuple[float, Path]]) -> float:
    """The typical gap between these frames, in seconds.

    Measured rather than read from the setting: a preserved incident may have
    been recorded at an interval nobody uses any more, and playing it back at
    today's speed would misdate every frame in it.
    """
    if len(found) < 2:
        return float(interval())
    gaps = sorted(found[i + 1][0] - found[i][0] for i in range(len(found) - 1))
    middle = gaps[len(gaps) // 2]
    return max(float(MIN_INTERVAL), min(float(MAX_INTERVAL), middle))


def render(found: list[tuple[float, Path]]) -> tuple[bytes | None, str]:
    """Render frames as a timelapse MP4. Never raises.

    Returns (mp4_bytes, note). The bytes are held in memory rather than cached
    on disk: for the live ring the newest frame changes every few seconds, so a
    cache would be stale almost immediately, and even a twenty-minute clip is
    only a couple of MB.
    """
    if not camera.ffmpeg_available():
        return None, "ffmpeg is not installed"
    if not found:
        return None, "there is no recording for that period"
    if len(found) < 2:
        return None, "only one frame was recorded — there is nothing to play"

    gap = _spacing(found)
    hold_ceiling = gap * CLIP_MAX_HOLD
    start = int(found[0][0])

    entries = []
    for index, (taken, path) in enumerate(found):
        following = found[index + 1][0] if index + 1 < len(found) else taken + gap
        held = max(0.2, min(hold_ceiling, following - taken))
        # Single quotes are how the concat demuxer delimits a path, so any in
        # the path itself have to be broken out of and back in.
        safe = str(path).replace("'", "'\\''")
        entries.append(f"file '{safe}'\nduration {held:.3f}")

    with tempfile.TemporaryDirectory(prefix="godfather-clip-") as workspace:
        listing = Path(workspace) / "frames.txt"
        output = Path(workspace) / "clip.mp4"
        try:
            listing.write_text("\n".join(entries) + "\n")
        except OSError as exc:
            return None, str(exc)

        argv = camera.clip_argv(listing, output, start, gap * CLIP_FPS, CLIP_FPS)
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=CLIP_TIMEOUT)
        except subprocess.TimeoutExpired:
            return None, "rendering the clip took too long"
        except OSError as exc:
            return None, str(exc)

        if done.returncode != 0 or not output.is_file():
            tail = (done.stderr or "").strip().splitlines()
            return None, tail[-1][:200] if tail else "the clip could not be rendered"

        try:
            return output.read_bytes(), ""
        except OSError as exc:
            return None, str(exc)


def render_clip(window_minutes: float) -> tuple[bytes | None, str]:
    """The last `window_minutes` of the live recording."""
    return render(recent(window_minutes))


def sweep() -> int:
    """Delete frames past the retention window. Returns how many went."""
    found = frames()
    if not found:
        return 0

    cutoff = time.time() - minutes() * 60
    # A backstop against a clock jump or a wildly misconfigured interval: never
    # hold more than three windows' worth of frames however old they claim to be.
    ceiling = max(20, int(minutes() * 60 / max(1, interval())) * 3)

    doomed = [path for taken, path in found if taken < cutoff]
    if len(found) - len(doomed) > ceiling:
        keep_from = len(found) - ceiling
        doomed = [path for _, path in found[:keep_from]]

    gone = 0
    for path in doomed:
        try:
            path.unlink(missing_ok=True)
            gone += 1
        except OSError:
            continue
    return gone


def clear() -> None:
    for _, path in frames():
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue


# ---------------------------------------------------------------- the stream


def reap_orphans() -> int:
    """Kill ffmpeg streams left behind by a God Father that did not exit.

    SIGKILL does not propagate to children and a power cut signals nothing, so a
    hard stop can leave ffmpeg alive and holding the camera. Since a camera can
    only be opened by one program at a time, that orphan would lock the next
    God Father out of recording indefinitely — a silent, permanent failure of
    the one feature that exists to survive hard stops.

    Identified by the ring pattern in the command line, which no other ffmpeg on
    the machine has any reason to carry, so nothing else can be caught by it.
    """
    marker = str(RING_DIR / PATTERN)
    mine = os.getpid()
    killed = []

    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return 0

    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == mine:
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if marker not in cmdline or "ffmpeg" not in cmdline:
            continue
        try:
            os.kill(int(entry.name), signal.SIGTERM)
            killed.append(int(entry.name))
        except OSError:
            continue

    # The device is not free the instant SIGTERM is sent, and the whole point is
    # to be able to open it next, so wait for them to actually go.
    deadline = time.time() + STOP_GRACE
    for pid in killed:
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.1)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    return len(killed)


class Ring:
    """Keeps one ffmpeg alive writing frames, and sweeps behind it."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._supervisor: threading.Thread | None = None
        self._sweeper: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._device = ""
        self._error = ""
        self._restarts = 0
        self._started_at = 0.0

    # -- lifecycle

    def start(self, device: str) -> None:
        """Begin recording, or re-point an already running stream at `device`."""
        with self._lock:
            if self._supervisor is not None and self._supervisor.is_alive():
                if device == self._device:
                    return
                self._device = device
                self._kill_current()  # the supervisor will respawn on the new device
                return

            self._device = device
            self._stop.clear()
            self._error = ""
            self._restarts = 0
            self._started_at = time.time()
            self._supervisor = threading.Thread(
                target=self._supervise, name="ring-stream", daemon=True
            )
            self._supervisor.start()
            self._sweeper = threading.Thread(target=self._sweep_loop, name="ring-sweep", daemon=True)
            self._sweeper.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._kill_current()
            supervisor, self._supervisor = self._supervisor, None
            sweeper, self._sweeper = self._sweeper, None
        for thread in (supervisor, sweeper):
            if thread is not None:
                thread.join(timeout=STOP_GRACE + 2)

    def restart(self) -> None:
        """Bounce the stream so a changed interval or device takes effect."""
        with self._lock:
            if self._supervisor is None or not self._supervisor.is_alive():
                return
            self._kill_current()

    def _kill_current(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=STOP_GRACE)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
        except OSError:
            pass

    # -- state

    def running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def error(self) -> str:
        return self._error

    def restarts(self) -> int:
        return self._restarts

    def newest_age(self) -> float | None:
        """Seconds since the last frame landed, or None if there is no frame."""
        latest = newest()
        return None if latest is None else max(0.0, time.time() - latest[0])

    def healthy(self) -> bool:
        """Recording *and* actually producing frames.

        The distinction earns its keep: a stream can be alive and silently
        producing nothing, which is the failure that looks like success.
        """
        if not self.running():
            return False
        age = self.newest_age()
        if age is None:
            # Nothing yet — fine only while the sensor is still warming up.
            return time.time() - self._started_at < interval() + 10
        return age <= interval() * 3 + 5

    # -- the threads

    def _supervise(self) -> None:
        try:
            self._supervise_until_stopped()
        finally:
            # However this thread leaves — stop, exception, anything — it must
            # not leave an ffmpeg behind. An abandoned stream holds the camera,
            # and a held camera is the one failure this whole design exists to
            # avoid. stop() kills the process too, but it cannot kill one that
            # _spawn had not yet assigned when it looked.
            self._kill_current()

    def _supervise_until_stopped(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            device = self._device
            if not device or not camera.valid_device(device):
                self._error = f"{device or 'no device'} is not a /dev/videoN device"
                if self._stop.wait(BACKOFF[-1]):
                    return
                continue

            started = time.time()
            if not self._spawn(device):
                attempt = min(attempt + 1, len(BACKOFF) - 1)
                if self._stop.wait(BACKOFF[attempt]):
                    return
                continue

            proc = self._proc
            while proc is not None and proc.poll() is None and not self._stop.is_set():
                self._stop.wait(1.0)
            if self._stop.is_set():
                return

            self._error = self._why_it_died(device)
            # A stream that ran a good while then died is a hiccup worth
            # retrying at once; one that dies immediately is a real fault and
            # should back off instead of spinning on the device.
            if time.time() - started > 60:
                attempt = 0
            else:
                attempt = min(attempt + 1, len(BACKOFF) - 1)
            self._restarts += 1
            if self._stop.wait(BACKOFF[attempt] if attempt else 1):
                return

    def _spawn(self, device: str) -> bool:
        # Checked again here, and again after the reap: stop() may have been
        # called since the loop last looked, and spawning into a shutdown is how
        # an orphan gets made.
        if self._stop.is_set():
            return False
        reap_orphans()
        if self._stop.is_set():
            return False
        argv = camera.stream_argv(device, interval(), str(RING_DIR / PATTERN))
        try:
            STREAM_LOG.parent.mkdir(parents=True, exist_ok=True)
            # Truncated per spawn, and ffmpeg writes to it directly, so a stream
            # left running for weeks cannot fill a pipe nobody is draining.
            log = open(STREAM_LOG, "w")
        except OSError:
            log = subprocess.DEVNULL  # type: ignore[assignment]

        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log,
            )
        except OSError as exc:
            self._proc = None
            self._error = str(exc)
            return False
        finally:
            if hasattr(log, "close"):
                try:
                    log.close()
                except OSError:
                    pass
        self._error = ""
        return True

    def _why_it_died(self, device: str) -> str:
        try:
            tail = STREAM_LOG.read_text(errors="replace").strip()
        except OSError:
            tail = ""
        return camera.explain(tail, device) if tail else "the camera stream stopped"

    def _sweep_loop(self) -> None:
        while not self._stop.wait(SWEEP_SECONDS):
            try:
                sweep()
            except Exception:
                continue


ring = Ring()
