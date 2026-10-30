"""Which application uses which AI model, and how much."""

from fastapi import APIRouter, HTTPException

from .. import ai_monitor as ai
from ..models import (
    AiAppRow,
    AiCall,
    AiDeclareIn,
    AiLiveRow,
    AiLocalModel,
    AiModelRow,
    AiPoint,
    AiState,
    AiTotals,
    AiUsageIn,
)

router = APIRouter(prefix="/api/ai", tags=["ai"])

NO_PSUTIL = (
    "psutil is not installed, so open connections cannot be read. Detection and "
    "reported usage still work. Install it with:  pip install psutil"
)


@router.get("", response_model=AiState)
def state(minutes: int = 60, calls: int = 80) -> AiState:
    minutes = max(5, min(minutes, 1440))
    calls = max(10, min(calls, 500))

    ollama = ai.ollama_state()

    return AiState(
        available=True,
        unavailable_reason="" if ai.psutil is not None else NO_PSUTIL,
        monitoring=ai.monitor.running,
        last_sample=ai.monitor.last_run,
        last_error=ai.monitor.last_error,
        sample_seconds=ai.SAMPLE_SECONDS,
        retention_hours=ai.RETENTION_HOURS,
        window_minutes=minutes,
        totals=AiTotals(**ai.totals(minutes)),
        apps=[AiAppRow(**row) for row in ai.by_app(minutes)],
        models=[AiModelRow(**row) for row in ai.by_model(minutes)],
        live=[AiLiveRow(**row) for row in ai.live()],
        ollama_reachable=bool(ollama.get("reachable")),
        local_models=[AiLocalModel(**row) for row in ollama.get("models", [])],
        points=[AiPoint(**point) for point in ai.history(minutes)],
        calls=[AiCall(**row) for row in ai.recent_calls(calls)],
        known_addresses=ai.addresses.count,
    )


@router.post("/usage", response_model=AiCall, status_code=201)
def report(usage: AiUsageIn) -> AiCall:
    """Report one model call.

    The accurate path, and the only one that can know a token count without
    guessing: the application already has the number its SDK returned, and this
    is where it puts it.
    """
    app_id, app_name = ai.resolve_app(usage.app, usage.app_id, usage.port)
    row_id = ai.record(
        app_id=app_id,
        app_name=app_name,
        provider=usage.provider,
        model=usage.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        latency_ms=usage.latency_ms,
        ok=usage.ok,
        error=usage.error,
        source="ingest",
        endpoint=usage.endpoint,
        ts=usage.ts,
    )
    for row in ai.recent_calls(1):
        if row["id"] == row_id:
            return AiCall(**row)
    raise HTTPException(status_code=500, detail="the call was not stored")


@router.post("/scan", response_model=list[AiAppRow])
def scan(minutes: int = 60) -> list[AiAppRow]:
    """Re-read every application's folder now, rather than waiting for the timer."""
    ai.addresses.refresh(force=True)
    ai.rescan()
    return [AiAppRow(**row) for row in ai.by_app(max(5, min(minutes, 1440)))]


@router.post("/links", response_model=list[AiAppRow], status_code=201)
def add_link(body: AiDeclareIn, minutes: int = 60) -> list[AiAppRow]:
    """Write a model against an application by hand.

    The only source that can be right about an app which has not run yet, and
    the one that outranks every guess below it.
    """
    if not body.provider and not body.model:
        raise HTTPException(status_code=422, detail="give a provider, a model, or both")
    provider = body.provider or ai.provider_of_model(body.model)
    if provider and provider not in ai.PROVIDERS:
        known = ", ".join(sorted(ai.PROVIDERS))
        raise HTTPException(status_code=422, detail=f"unknown provider. Known: {known}")
    ai.declare(body.app_id, provider, body.model, body.note)
    return [AiAppRow(**row) for row in ai.by_app(max(5, min(minutes, 1440)))]


@router.delete("/links/{link_id}", status_code=204)
def remove_link(link_id: int) -> None:
    """Only a declared link can be removed — the other two are re-derived."""
    ai.undeclare(link_id)


@router.get("/providers")
def providers() -> list[dict]:
    """What God Father knows how to recognise, so the UI need not hard-code it."""
    return [
        {
            "key": provider.key,
            "label": provider.label,
            "local": provider.local,
            "hosts": list(provider.hosts),
            "ports": list(provider.ports),
            "env_keys": list(provider.env_keys),
        }
        for provider in ai.PROVIDERS.values()
    ]


@router.delete("/calls", status_code=204)
def clear() -> None:
    ai.clear_calls()
