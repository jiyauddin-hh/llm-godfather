"""Talk to this machine's webcam through ffmpeg.

Two ways of using it, and the difference matters:

`stream_argv` builds a *long-lived* ffmpeg that holds the camera open and
writes one frame every few seconds — the rolling recording that `ring.py`
supervises. This is the normal path.

`capture` opens the camera, takes exactly one frame and closes it. Only
usable when the stream is *not* running, because a camera can be opened by
one program at a time and the stream is a program.

The one thing that is not obvious in either path: a V4L2 webcam's first frames
are garbage. Auto-exposure has not settled, and on this class of USB camera
frame zero comes back uniformly black — measured at a mean luma of 0.0 out of
255. Asking ffmpeg for the first frame therefore yields a black rectangle every
single time, which looks exactly like a camera that is working and a room that
is dark. Discarding the first WARMUP_FRAMES fixes it, at the cost of the second
or so it takes the sensor to settle — a cost the stream pays once at startup,
after which every frame comes off a sensor that is already warm.
"""

import glob
import re
import shutil
import subprocess
from pathlib import Path

FFMPEG = "ffmpeg"

# Ten frames is where exposure settles on the camera this was built against
# (mean luma 0.0 at frame 0, 83 by frame 5, 99 by frame 10, and no better
# after). Roughly 1.7s door to door.
WARMUP_FRAMES = 10

# ffmpeg -q:v, 2 = best … 31 = worst. 5 keeps a 640x480 still around 40 KB.
QUALITY = 5

# Generous: covers a slow camera without ever hanging a shutdown.
TIMEOUT_SECONDS = 12.0

_VALID_DEVICE = re.compile(r"^/dev/video\d+$")


def ffmpeg_available() -> bool:
    return shutil.which(FFMPEG) is not None


def devices() -> list[str]:
    """Capture devices, lowest number first — /dev/video0 is the usual one."""

    def index(path: str) -> int:
        match = re.search(r"(\d+)$", path)
        return int(match.group(1)) if match else 0

    return sorted(glob.glob("/dev/video*"), key=index)


def default_device() -> str | None:
    found = devices()
    return found[0] if found else None


def unavailable_reason() -> str | None:
    """Why a capture could not possibly work, or None if it should."""
    if not ffmpeg_available():
        return "ffmpeg is not installed — install it with: sudo apt install ffmpeg"
    if not devices():
        return "no camera found (no /dev/video* device)"
    return None


def explain(stderr: str, device: str) -> str:
    """Turn ffmpeg's noise into something worth showing a person."""
    low = stderr.lower()
    if "busy" in low or "resource temporarily unavailable" in low:
        return (
            f"{device} is in use by another program. A camera can only be opened by one "
            "program at a time — close whatever is watching it and the next capture will work."
        )
    if "permission denied" in low:
        return f"permission denied opening {device} — your user needs to be in the 'video' group"
    if "no such file" in low or "cannot find" in low:
        return f"{device} does not exist"
    if "immediate exit requested" in low or not stderr:
        return "the camera did not produce a frame in time"
    return stderr.splitlines()[-1][:200]


def valid_device(device: str) -> bool:
    return bool(_VALID_DEVICE.match(device))


def stream_argv(device: str, interval_seconds: float, pattern: str) -> list[str]:
    """ffmpeg that holds the camera open and drops a JPEG every `interval_seconds`.

    `pattern` is an strftime template, so each frame names its own wall-clock
    time and a plain directory listing comes out in chronological order. Two
    flags carry more weight than they look like they do:

    `-atomic_writing 1` writes to a temporary file and renames it into place.
    Rename is atomic on ext4, so a power cut can never leave a half-written JPEG
    behind — which matters when the whole point of the recording is to survive
    exactly that. Without it the newest frame, the one worth having, is the one
    most likely to be truncated.

    `-update 0` keeps ffmpeg writing a new file per frame rather than
    overwriting one; the sweep in ring.py is what bounds the folder.
    """
    return [
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-f", "v4l2", "-i", device,
        # Warm-up first, then thin to one frame per interval. Ordering matters:
        # thinning first would let frame zero — the black one — through.
        "-vf", f"select='gte(n\\,{WARMUP_FRAMES})',fps=1/{interval_seconds:g}",
        "-q:v", str(QUALITY),
        "-f", "image2", "-strftime", "1", "-atomic_writing", "1", "-update", "0",
        pattern,
    ]


# Where to find a monospaced face for the timestamp burnt into a clip. First
# one that exists wins; if none do, the clip is rendered without the overlay
# rather than not rendered at all.
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
)


def clip_font() -> str | None:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def clip_argv(list_file: Path, out_file: Path, start_epoch: int, speed: float, fps: int) -> list[str]:
    """ffmpeg that turns a list of stills into a timelapse with the clock burnt in.

    Three parts of this are less arbitrary than they look:

    `%{pts:localtime:START}` draws the *real* wall-clock time of each frame.
    That works only because the concat list gives every frame its true duration,
    so a frame's presentation timestamp is genuinely how long after the first
    frame it was taken. drawtext sits before setpts in the chain and therefore
    reads the original timestamps, not the sped-up ones.

    `setpts=PTS/speed` is what makes it a timelapse, where speed is the interval
    times the output rate — so one recorded frame becomes exactly one frame of
    video and the clock never advances mid-frame.

    CFR output. Passthrough drops the final frame, which has nothing after it to
    give it a duration — and the final frame is the most recent thing that
    happened, so losing it is precisely the wrong frame to lose.
    """
    font = clip_font()
    stamp = (
        rf"drawtext=fontfile={font}:text='%{{pts\:localtime\:{start_epoch}}}':"
        "x=12:y=h-34:fontsize=20:fontcolor=white:"
        "box=1:boxcolor=black@0.55:boxborderw=7,"
        if font
        else ""
    )
    return [
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-vf", f"{stamp}setpts=PTS/{speed:g},format=yuv420p",
        "-r", str(fps), "-fps_mode", "cfr",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
        "-movflags", "+faststart",
        "-y", str(out_file),
    ]


def capture(path: Path, device: str | None = None) -> tuple[bool, str]:
    """Write one JPEG to `path`. Returns (ok, note); never raises."""
    if not ffmpeg_available():
        return False, "ffmpeg is not installed"

    device = (device or "").strip() or default_device()
    if not device:
        return False, "no camera found (no /dev/video* device)"
    if not _VALID_DEVICE.match(device):
        return False, f"{device!r} is not a /dev/videoN device"

    path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-f", "v4l2", "-i", device,
        # Throw away the frames the sensor produces before exposure settles.
        "-vf", f"select='gte(n\\,{WARMUP_FRAMES})'",
        "-frames:v", "1", "-q:v", str(QUALITY), "-y", str(path),
    ]

    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        path.unlink(missing_ok=True)
        return False, f"the camera did not respond within {TIMEOUT_SECONDS:.0f}s"
    except OSError as exc:
        return False, str(exc)

    if done.returncode != 0 or not path.exists():
        note = explain(done.stderr or "", device)
        path.unlink(missing_ok=True)
        return False, note

    size = path.stat().st_size
    # A frame this small is the all-black one warm-up is meant to avoid. Keep
    # it — a black photo is still evidence of the time — but say what it is.
    if size < 5_000:
        return True, "captured, but the frame is very dark — the lens may be covered"

    return True, ""
