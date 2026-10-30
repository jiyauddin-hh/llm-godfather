"""Process supervision: launching apps, capturing their output, restarting them."""

import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from .config import (
    CRASH_WINDOW_SECONDS,
    LOG_BUFFER_LINES,
    MAX_LOG_BYTES,
    MAX_RAPID_RESTARTS,
    STOP_GRACE_SECONDS,
    log_path,
)
from .database import record_event
from .news import news
from .ports import port_is_listening, process_stats
from .witness import witness

IS_WINDOWS = sys.platform.startswith("win")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class ManagedProcess:
    """One supervised child process plus its rolling log buffer."""

    def __init__(self, app_id: int, name: str, loop: asyncio.AbstractEventLoop):
        self.app_id = app_id
        self.name = name
        self.loop = loop

        self.proc: subprocess.Popen | None = None
        self.status = "stopped"
        self.started_at: float | None = None
        self.exit_code: int | None = None
        self.last_error: str | None = None

        self.restart_count = 0
        self.auto_restart = False
        self._recent_restarts: deque[float] = deque(maxlen=MAX_RAPID_RESTARTS)
        self._stopping = False

        self.buffer: deque[dict] = deque(maxlen=LOG_BUFFER_LINES)
        self.subscribers: set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------ logs

    def emit(self, stream: str, text: str) -> None:
        """Record one log line: memory buffer, disk, and every live subscriber."""
        line = {"ts": _now(), "stream": stream, "text": text.rstrip("\n")}
        with self._lock:
            self.buffer.append(line)
        self._write_to_disk(line)
        try:
            news.from_line(self.app_id, stream, line["text"])
        except Exception:
            # The ticker is a reader; it must never cost an app its log.
            pass

        for queue in list(self.subscribers):
            try:
                self.loop.call_soon_threadsafe(queue.put_nowait, line)
            except RuntimeError:
                # Event loop already closed during shutdown.
                pass

    def _write_to_disk(self, line: dict) -> None:
        path = log_path(self.app_id)
        try:
            if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
                path.replace(path.with_suffix(".log.1"))
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{line['ts']}\t{line['stream']}\t{line['text']}\n")
        except OSError:
            pass

    def tail(self, limit: int = 500) -> list[dict]:
        with self._lock:
            buffered = list(self.buffer)
        if len(buffered) >= limit:
            return buffered[-limit:]

        # The file already contains everything in the buffer, so drop that
        # overlap before prepending the older lines it alone still holds.
        older = self._read_from_disk(limit)
        if buffered:
            older = older[: max(0, len(older) - len(buffered))]
        return (older + buffered)[-limit:]

    def _read_from_disk(self, limit: int) -> list[dict]:
        """Replay older lines that have already fallen out of the memory buffer."""
        path = log_path(self.app_id)
        if limit <= 0 or not path.exists():
            return []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                raw = deque(handle, maxlen=limit)
        except OSError:
            return []

        lines = []
        for entry in raw:
            parts = entry.rstrip("\n").split("\t", 2)
            if len(parts) == 3:
                lines.append({"ts": parts[0], "stream": parts[1], "text": parts[2]})
            else:
                lines.append({"ts": "", "stream": "stdout", "text": entry.rstrip("\n")})
        return lines

    def clear_logs(self) -> None:
        with self._lock:
            self.buffer.clear()
        try:
            log_path(self.app_id).unlink(missing_ok=True)
        except OSError:
            pass

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)

    # ------------------------------------------------------------- lifecycle

    @property
    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, command: str, working_dir: str, env: dict, auto_restart: bool) -> None:
        if self.is_alive:
            raise RuntimeError(f"'{self.name}' is already running (pid {self.proc.pid})")

        cwd = Path(working_dir).expanduser()
        if not cwd.is_dir():
            raise RuntimeError(f"working directory does not exist: {cwd}")

        self.auto_restart = auto_restart
        self._stopping = False
        self.exit_code = None
        self.last_error = None
        self.status = "starting"

        child_env = {**os.environ, **{str(k): str(v) for k, v in env.items()}}
        # Keep Python children from buffering their output away from us.
        child_env.setdefault("PYTHONUNBUFFERED", "1")

        popen_kwargs: dict = {
            "shell": True,
            "cwd": str(cwd),
            "env": child_env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "stdin": subprocess.DEVNULL,
            "text": True,
            "bufsize": 1,
            "errors": "replace",
        }
        if IS_WINDOWS:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            # Own session, so the whole tree can be signalled as one group.
            popen_kwargs["start_new_session"] = True

        try:
            self.proc = subprocess.Popen(command, **popen_kwargs)
        except OSError as exc:
            self.status = "crashed"
            self.last_error = str(exc)
            self.emit("system", f"failed to start: {exc}")
            record_event(self.app_id, "error", f"failed to start: {exc}")
            raise RuntimeError(str(exc)) from exc

        self.started_at = time.time()
        self.status = "running"
        self.emit("system", f"started `{command}` (pid {self.proc.pid}) in {cwd}")
        record_event(self.app_id, "started", f"pid {self.proc.pid}")
        witness.record(self.app_id, self.name, "started", f"pid {self.proc.pid}")

        self._threads = [
            threading.Thread(target=self._pump, args=(self.proc.stdout, "stdout"), daemon=True),
            threading.Thread(target=self._pump, args=(self.proc.stderr, "stderr"), daemon=True),
            threading.Thread(
                target=self._watch_exit,
                args=(self.proc, command, working_dir, env),
                daemon=True,
            ),
        ]
        for thread in self._threads:
            thread.start()

    def _pump(self, pipe, stream: str) -> None:
        try:
            for line in iter(pipe.readline, ""):
                self.emit(stream, line)
        except (ValueError, OSError):
            pass
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    def _watch_exit(self, proc: subprocess.Popen, command: str, working_dir: str, env: dict) -> None:
        code = proc.wait()

        # A newer start already replaced this process — nothing to report.
        if self.proc is not proc:
            return

        self.exit_code = code
        self.started_at = None

        if self._stopping:
            self.status = "stopped"
            self.emit("system", f"stopped (exit code {code})")
            record_event(self.app_id, "stopped", f"exit code {code}")
            witness.record(self.app_id, self.name, "stopped", f"exit code {code}")
            return

        self.status = "stopped" if code == 0 else "crashed"
        self.emit("system", f"process exited with code {code}")
        record_event(self.app_id, self.status, f"exit code {code}")
        witness.record(self.app_id, self.name, self.status, f"exit code {code}")

        if self.auto_restart and self._may_restart():
            self.restart_count += 1
            self._recent_restarts.append(time.time())
            self.emit("system", f"auto-restarting (attempt {self.restart_count})")
            record_event(self.app_id, "restarted", f"attempt {self.restart_count}")
            time.sleep(1.5)
            try:
                self.start(command, working_dir, env, self.auto_restart)
            except RuntimeError:
                pass

    def _may_restart(self) -> bool:
        """Stop flapping: give up after MAX_RAPID_RESTARTS inside the crash window."""
        cutoff = time.time() - CRASH_WINDOW_SECONDS
        recent = [stamp for stamp in self._recent_restarts if stamp > cutoff]
        if len(recent) >= MAX_RAPID_RESTARTS:
            self.auto_restart = False
            self.last_error = "auto-restart disabled after repeated rapid crashes"
            self.emit("system", self.last_error)
            record_event(self.app_id, "error", self.last_error)
            return False
        return True

    def stop(self, grace: float = STOP_GRACE_SECONDS) -> None:
        if not self.is_alive:
            self.status = "stopped"
            return

        self._stopping = True
        self.auto_restart = False
        proc = self.proc
        assert proc is not None

        self.emit("system", "stopping…")
        try:
            if IS_WINDOWS:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            proc.terminate()

        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self.emit("system", "did not exit in time — sending SIGKILL")
            try:
                if IS_WINDOWS:
                    proc.kill()
                else:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass

        self.status = "stopped"
        self.started_at = None

    # ---------------------------------------------------------------- status

    def snapshot(self, port: int | None = None) -> dict:
        alive = self.is_alive
        cpu = memory = None
        if alive and self.proc is not None:
            cpu, memory = process_stats(self.proc.pid)

        return {
            "status": self.status if alive or self.status != "running" else "stopped",
            "pid": self.proc.pid if alive and self.proc else None,
            "started_at": (
                datetime.fromtimestamp(self.started_at, timezone.utc).isoformat(timespec="seconds")
                if alive and self.started_at
                else None
            ),
            "uptime_seconds": round(time.time() - self.started_at, 1) if alive and self.started_at else None,
            "exit_code": self.exit_code,
            "restart_count": self.restart_count,
            "cpu_percent": cpu,
            "memory_mb": memory,
            "port_open": port_is_listening(port) if port else None,
            "last_error": self.last_error,
        }


class ProcessManager:
    """Registry of supervised processes, keyed by app id."""

    def __init__(self) -> None:
        self._processes: dict[int, ManagedProcess] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    def get(self, app_id: int, name: str = "") -> ManagedProcess:
        if app_id not in self._processes:
            if self._loop is None:
                self._loop = asyncio.get_event_loop()
            self._processes[app_id] = ManagedProcess(app_id, name or f"app-{app_id}", self._loop)
        elif name:
            self._processes[app_id].name = name
        return self._processes[app_id]

    def peek(self, app_id: int) -> ManagedProcess | None:
        return self._processes.get(app_id)

    def forget(self, app_id: int) -> None:
        process = self._processes.pop(app_id, None)
        if process and process.is_alive:
            process.stop()

    def running_ids(self) -> list[int]:
        return [app_id for app_id, proc in self._processes.items() if proc.is_alive]

    def stop_all(self) -> None:
        for process in list(self._processes.values()):
            if process.is_alive:
                try:
                    process.stop(grace=4)
                except Exception:
                    pass


manager = ProcessManager()
