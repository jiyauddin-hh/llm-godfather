"""Focus Mode: give one application the machine, then put everything back."""

from fastapi import APIRouter, HTTPException

from .. import focus

router = APIRouter(prefix="/api/focus", tags=["focus"])


@router.get("")
def focus_status() -> dict:
    """Whether Focus Mode is on, on which app, and the step-by-step log."""
    return focus.status()


@router.post("/preview/{app_id}")
def focus_preview(app_id: int) -> dict:
    """What Focus Mode would stop, keep, start and unload. Changes nothing."""
    try:
        return focus.plan(app_id)
    except focus.FocusError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/start/{app_id}")
def focus_start(app_id: int) -> dict:
    """Start Focus Mode. Runs in the background; poll GET /api/focus for progress."""
    try:
        return focus.start(app_id)
    except focus.FocusError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/exit")
def focus_exit() -> dict:
    """Restore what Focus Mode stopped. Runs in the background like start."""
    try:
        return focus.exit_focus()
    except focus.FocusError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
