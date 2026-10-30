"""Runtime configuration and on-disk locations."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.environ.get("GODFATHER_DATA_DIR", BASE_DIR / "data"))
LOG_DIR = DATA_DIR / "logs"
CAPTURE_DIR = DATA_DIR / "captures"
# The rolling recording. Separate from CAPTURE_DIR because these frames are
# disposable — swept on a timer — while a capture is a permanent record.
RING_DIR = DATA_DIR / "ring"
# Frames rescued from the ring when something happened that the ring itself
# will not survive — a power cut, whose lead-up would otherwise be swept within
# seconds of the next boot. One folder per incident, kept until its capture is.
INCIDENT_DIR = DATA_DIR / "incidents"
DB_PATH = DATA_DIR / "godfather.db"

# How many recent log lines are kept in memory per app for instant replay.
LOG_BUFFER_LINES = int(os.environ.get("GODFATHER_LOG_BUFFER", "3000"))

# A log file is rotated to <name>.1 once it grows past this.
MAX_LOG_BYTES = int(os.environ.get("GODFATHER_MAX_LOG_BYTES", str(5 * 1024 * 1024)))

# Graceful shutdown window before a process gets SIGKILL.
STOP_GRACE_SECONDS = float(os.environ.get("GODFATHER_STOP_GRACE", "8"))

# Auto-restart guardrails: at most N restarts inside the crash window.
MAX_RAPID_RESTARTS = int(os.environ.get("GODFATHER_MAX_RESTARTS", "5"))
CRASH_WINDOW_SECONDS = 60.0

CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
]

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
RING_DIR.mkdir(parents=True, exist_ok=True)
INCIDENT_DIR.mkdir(parents=True, exist_ok=True)


def log_path(app_id: int) -> Path:
    return LOG_DIR / f"app-{app_id}.log"


def capture_path(name: str) -> Path:
    """Resolve a stored capture filename, refusing anything with a path in it."""
    return CAPTURE_DIR / Path(name).name


def ring_path(name: str) -> Path:
    """Resolve a ring frame filename, refusing anything with a path in it."""
    return RING_DIR / Path(name).name


def incident_path(name: str) -> Path:
    """Resolve a preserved incident folder, refusing anything with a path in it."""
    return INCIDENT_DIR / Path(name).name
