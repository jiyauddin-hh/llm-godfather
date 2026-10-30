"""Focus Mode: give one application the machine.

Pick an app and God Father decides the rest:

- keep    the app, everything it needs (followed all the way down), and every
          protected app
- stop    every other app God Father is running
- unload  every Ollama model the kept apps do not use — that is where the VRAM
          goes, and VRAM is usually what the focused app is short of
- leave   apps running outside God Father. God Father did not start them and
          holds none of their processes, so it cannot stop them cleanly; they
          are listed as "not controlled" rather than killed

Before anything is touched, what was running is saved to the settings table,
so Exit Focus Mode can put it back — even after God Father itself restarts.
Every step lands in the Events log of the app it affected.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from .database import db, get_setting, record_event, set_setting
from .manager import manager
from .network import reapply_relay
from .ports import port_is_listening
from .service import clear_own_relay, needs_of, runtime_for

SETTING_KEY = "focus_state"

# XTTS and other model servers take a while to load before their port opens.
START_TIMEOUT_SECONDS = 120
# A model counts as pinned when Ollama would keep it longer than this; only
# pinned models are reloaded on exit — the rest would have expired anyway.
PINNED_AFTER_SECONDS = 24 * 3600

_lock = threading.Lock()
_worker: threading.Thread | None = None


class FocusError(Exception):
    pass


# ------------------------------------------------------------------- state

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state() -> dict:
    try:
        state = json.loads(get_setting(SETTING_KEY, "") or "{}")
    except json.JSONDecodeError:
        state = {}
    return state if isinstance(state, dict) else {}


def _save(state: dict) -> None:
    set_setting(SETTING_KEY, json.dumps(state))


def _log(state: dict, text: str, level: str = "info") -> None:
    state.setdefault("log", []).append({"ts": _now(), "level": level, "text": text})
    state["log"] = state["log"][-200:]
    _save(state)


def is_active() -> bool:
    return bool(load_state().get("active"))


def stopped_ids() -> set[int]:
    """Apps Focus Mode switched off. Autostart must not bring these back."""
    state = load_state()
    if not state.get("active"):
        return set()
    return {item["id"] for item in state.get("stopped", [])}


def recover() -> None:
    """After a God Father restart, a half-finished step is no longer running."""
    state = load_state()
    if state.get("phase") in ("starting", "restoring"):
        state["phase"] = "active" if state.get("active") else "ended"
        _log(state, "God Father restarted in the middle of this step; it did not finish. "
                    "Use Exit Focus Mode to restore.", "warn")


# ------------------------------------------------------------------ ollama

def _ollama_base() -> str:
    host = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434").replace("http://", "").strip("/")
    return f"http://{host}"


def _ollama(path: str, payload: dict | None = None, timeout: float = 5.0) -> dict | None:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        _ollama_base() + path, data=data,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace") or "{}")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def _loaded_models() -> list[dict]:
    """Models in memory right now, with whether each one is pinned there."""
    running = _ollama("/api/ps")
    if running is None:
        return []
    out = []
    for entry in running.get("models", []):
        pinned = False
        expires = entry.get("expires_at") or ""
        try:
            stamp = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            pinned = (stamp - datetime.now(timezone.utc)).total_seconds() > PINNED_AFTER_SECONDS
        except ValueError:
            pass
        out.append({
            "name": entry.get("name") or entry.get("model", ""),
            "vram": int(entry.get("size_vram") or 0),
            "size": int(entry.get("size") or 0),
            "pinned": pinned,
        })
    return out


def _model_matches(loaded: str, wanted: set[str]) -> bool:
    """'llama3.1:8b' matches 'llama3.1:8b'; 'llama3.1' matches 'llama3.1:latest'."""
    return loaded in wanted or loaded.removesuffix(":latest") in wanted


def _unload(name: str) -> bool:
    # keep_alive 0 asks Ollama to drop the model now; it reloads on next use.
    return _ollama("/api/generate", {"model": name, "keep_alive": 0}, timeout=30) is not None


def _reload_pinned(name: str) -> bool:
    return _ollama("/api/generate", {"model": name, "keep_alive": -1}, timeout=120) is not None


# --------------------------------------------------------------- deciding

def _all_rows() -> dict[int, dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM apps ORDER BY name COLLATE NOCASE").fetchall()
    return {row["id"]: dict(row) for row in rows}


def _closure(start: int, rows: dict[int, dict]) -> list[int]:
    """The app and everything it needs, dependencies before dependents."""
    order: list[int] = []
    seen: set[int] = set()

    def visit(app_id: int) -> None:
        if app_id in seen or app_id not in rows:
            return
        seen.add(app_id)
        for dep in needs_of(rows[app_id]):  # type: ignore[arg-type]
            visit(dep)
        order.append(app_id)

    visit(start)
    return order


def _dependency_order(ids: list[int], rows: dict[int, dict]) -> list[int]:
    """`ids` sorted so that each app comes after anything it needs."""
    wanted = set(ids)
    order: list[int] = []
    seen: set[int] = set()

    def visit(app_id: int) -> None:
        if app_id in seen:
            return
        seen.add(app_id)
        for dep in needs_of(rows[app_id]):  # type: ignore[arg-type]
            if dep in wanted and dep in rows:
                visit(dep)
        order.append(app_id)

    for app_id in ids:
        visit(app_id)
    return order


def _who_needs(rows: dict[int, dict], keep: list[int]) -> dict[int, str]:
    """For each dependency, the name of a kept app that needs it."""
    out: dict[int, str] = {}
    for app_id in keep:
        for dep in needs_of(rows[app_id]):  # type: ignore[arg-type]
            out.setdefault(dep, rows[app_id]["name"])
    return out


def _is_tunnel(row: dict) -> bool:
    return "cloudflared" in row["command"] or "ngrok" in row["command"]


def plan(app_id: int) -> dict:
    """What Focus Mode would do for this app. Touches nothing."""
    rows = _all_rows()
    if app_id not in rows:
        raise FocusError(f"application {app_id} not found")
    target = rows[app_id]

    chain = _closure(app_id, rows)
    protected = [i for i, r in rows.items() if r["protected"] and i not in chain]
    keep_ids = chain + protected
    needed_by = _who_needs(rows, chain)

    status = {i: runtime_for(i, r["port"]) for i, r in rows.items()}

    keep, stop, outside = [], [], []
    for i in keep_ids:
        r, rt = rows[i], status[i]
        if i == app_id:
            reason = "the app you are focusing on"
        elif i in chain:
            reason = f"needed by {needed_by.get(i, target['name'])}"
        else:
            reason = "protected"
        if rt.status == "running":
            action = "keep running"
        elif rt.status == "external":
            action = "already running outside God Father"
        elif i in chain:
            action = "start"
        else:
            action = "leave stopped"
        keep.append({"id": i, "name": r["name"], "status": rt.status, "reason": reason, "action": action,
                     "port": r["port"]})

    warnings = []
    for i, r in rows.items():
        if i in keep_ids:
            continue
        rt = status[i]
        if rt.status == "running":
            item = {"id": i, "name": r["name"], "status": rt.status, "port": r["port"],
                    "memory_mb": rt.memory_mb or 0, "warning": None}
            if _is_tunnel(r):
                item["warning"] = "Tunnel: its public address stops working until you exit Focus Mode."
            stop.append(item)
        elif rt.status == "external":
            outside.append({"id": i, "name": r["name"], "port": r["port"],
                            "reason": "runs outside God Father, so it is left running"})

    if any(s["warning"] for s in stop):
        warnings.append(f"{sum(1 for s in stop if s['warning'])} tunnel(s) will go offline — "
                        "anyone using those public links loses access until you exit.")
    for k in keep:
        if k["action"] == "start" and k["port"] and port_is_listening(k["port"]):
            warnings.append(f"{k['name']} needs port {k['port']}, which something else already holds.")

    wanted_models = set()
    for i in chain:
        wanted_models |= {m.strip() for m in rows[i]["uses_models"].split(",") if m.strip()}
    loaded = _loaded_models()
    unload = [m for m in loaded if not _model_matches(m["name"], wanted_models)]
    kept_models = [m for m in loaded if _model_matches(m["name"], wanted_models)]

    if not _ollama("/api/version") and wanted_models:
        warnings.append("Ollama is not answering, so no models can be unloaded.")

    from .system_monitor import gpu  # deferred: shells out to nvidia-smi
    card = gpu()

    state = load_state()
    return {
        "target": {"id": app_id, "name": target["name"]},
        "keep": keep,
        "stop": stop,
        "not_controlled": outside,
        "unload_models": unload,
        "keep_models": [m["name"] for m in kept_models],
        "wanted_models": sorted(wanted_models),
        "freed": {
            "ram_mb": round(sum(s["memory_mb"] for s in stop), 1),
            "vram_bytes": sum(m["vram"] for m in unload),
        },
        "gpu": {"vram_used": card.get("vram_used", 0), "vram_total": card.get("vram_total", 0)} if card else None,
        "warnings": warnings,
        "blocked": "Focus Mode is already on. Exit it first." if state.get("active") else None,
    }


# ----------------------------------------------------------------- acting

def _start_row(row: dict) -> str | None:
    """Start an app the same way the Start button does. Returns an error or None."""
    process = manager.get(row["id"], row["name"])
    if process.is_alive:
        return None
    owner = clear_own_relay(row["id"], row["port"])
    if owner:
        return f"port {row['port']} is held by {owner.get('process') or 'another process'}"
    try:
        env = json.loads(row["env"])
    except (json.JSONDecodeError, TypeError):
        env = {}
    try:
        process.start(row["command"], row["working_dir"], env, bool(row["auto_restart"]))
    except RuntimeError as exc:
        return str(exc)
    if row["share_lan"] and row["port"]:
        time.sleep(1.0)
        reapply_relay(row["id"], row["port"], manager.loop)
    return None


def _wait_ready(row: dict, timeout: float = START_TIMEOUT_SECONDS) -> bool:
    """Until its port answers, or — with no port — until it has stayed up a moment."""
    process = manager.get(row["id"], row["name"])
    deadline = time.monotonic() + timeout
    if not row["port"]:
        time.sleep(2)
        return process.is_alive
    while time.monotonic() < deadline:
        if port_is_listening(row["port"]):
            return True
        if not process.is_alive:
            return False
        time.sleep(1)
    return False


def _run(fn) -> None:
    global _worker
    _worker = threading.Thread(target=fn, name="godfather-focus", daemon=True)
    _worker.start()


def _busy() -> bool:
    return _worker is not None and _worker.is_alive()


def start(app_id: int) -> dict:
    with _lock:
        if _busy():
            raise FocusError("Focus Mode is still working on the previous step.")
        if load_state().get("active"):
            raise FocusError("Focus Mode is already on. Exit it first.")
        decided = plan(app_id)
        rows = _all_rows()
        state = {
            "active": True,
            "phase": "starting",
            "target": decided["target"],
            "started_at": _now(),
            "stopped": [{"id": s["id"], "name": s["name"]} for s in decided["stop"]],
            "started": [],
            "unloaded": [],
            "failed": [],
            "log": [],
        }
        _log(state, f"Focus Mode on {decided['target']['name']}: stopping {len(decided['stop'])} app(s), "
                    f"unloading {len(decided['unload_models'])} model(s).")
        record_event(app_id, "focus", f"Focus Mode started: {len(decided['stop'])} app(s) stopped")

    def work() -> None:
        target = decided["target"]["name"]
        for item in decided["stop"]:
            process = manager.peek(item["id"])
            if process and process.is_alive:
                try:
                    process.stop()
                    _log(state, f"Stopped {item['name']}.")
                    record_event(item["id"], "stopped", f"Focus Mode on {target}")
                except Exception as exc:  # keep going: one stubborn app must not strand the rest
                    state["failed"].append(item["name"])
                    _log(state, f"Could not stop {item['name']}: {exc}", "error")

        for model in decided["unload_models"]:
            if _unload(model["name"]):
                state["unloaded"].append({"name": model["name"], "pinned": model["pinned"]})
                _log(state, f"Unloaded model {model['name']} ({model['vram'] / 1e9:.1f} GB of VRAM).")
            else:
                _log(state, f"Could not unload model {model['name']}.", "warn")

        to_start = [k["id"] for k in decided["keep"] if k["action"] == "start"]
        for i in _dependency_order(to_start, rows):
            row = rows[i]
            _log(state, f"Starting {row['name']}…")
            error = _start_row(row)
            if error:
                state["failed"].append(row["name"])
                _log(state, f"Could not start {row['name']}: {error}", "error")
                continue
            state["started"].append({"id": i, "name": row["name"]})
            if _wait_ready(row):
                _log(state, f"{row['name']} is up" + (f" on port {row['port']}." if row["port"] else "."))
            else:
                state["failed"].append(row["name"])
                _log(state, f"{row['name']} did not come up in time — check its log.", "error")

        state["phase"] = "active"
        _log(state, f"Focus Mode is on: {target} has the machine."
                    + (f" Problems: {', '.join(state['failed'])}." if state["failed"] else ""),
             "warn" if state["failed"] else "info")

    _run(work)
    return state


def exit_focus() -> dict:
    with _lock:
        if _busy():
            raise FocusError("Focus Mode is still working on the previous step.")
        state = load_state()
        if not state.get("active"):
            raise FocusError("Focus Mode is not on.")
        state["phase"] = "restoring"
        state["failed"] = []
        _log(state, "Exiting Focus Mode: putting everything back.")
        target_id = state["target"]["id"]
        record_event(target_id, "focus", "Focus Mode exit requested")

    def work() -> None:
        rows = _all_rows()
        # First stop what Focus Mode started, so the GPU is free for the models
        # coming back. Only what it started: anything you started yourself stays.
        for item in reversed(state.get("started", [])):
            process = manager.peek(item["id"])
            if process and process.is_alive:
                process.stop()
                _log(state, f"Stopped {item['name']} (Focus Mode had started it).")
                record_event(item["id"], "stopped", "Focus Mode exit")

        for model in state.get("unloaded", []):
            if model.get("pinned"):
                _log(state, f"Reloading pinned model {model['name']}…")
                if _reload_pinned(model["name"]):
                    _log(state, f"Model {model['name']} is loaded again.")
                else:
                    _log(state, f"Could not reload model {model['name']}.", "warn")

        ids = [s["id"] for s in state.get("stopped", []) if s["id"] in rows]
        back = []
        for i in _dependency_order(ids, rows):
            row = rows[i]
            if manager.peek(i) and manager.peek(i).is_alive:
                back.append(row["name"])
                continue
            error = _start_row(row)
            if error:
                state["failed"].append(row["name"])
                _log(state, f"Could not restart {row['name']}: {error}", "error")
                continue
            record_event(i, "started", "Focus Mode exit")
            if _wait_ready(row, timeout=60):
                back.append(row["name"])
                _log(state, f"{row['name']} is back.")
            else:
                state["failed"].append(row["name"])
                _log(state, f"{row['name']} did not come back in time — check its log.", "error")

        missing = [s["name"] for s in state.get("stopped", []) if s["id"] not in rows]
        for name in missing:
            _log(state, f"{name} was deleted while Focus Mode was on, so it was not restarted.", "warn")

        state["active"] = False
        state["phase"] = "ended"
        state["ended_at"] = _now()
        state["restored"] = back
        _log(state, f"Focus Mode ended. {len(back)} app(s) restored"
                    + (f", {len(state['failed'])} failed: {', '.join(state['failed'])}." if state["failed"] else "."),
             "warn" if state["failed"] else "info")
        record_event(target_id, "focus", f"Focus Mode ended: {len(back)} restored, {len(state['failed'])} failed")

    _run(work)
    return state


def status() -> dict:
    state = load_state()
    state["busy"] = _busy()
    return state
