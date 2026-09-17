"""Liveness for the whole stack, not just this process.

`/health` reports the database **and** the model. Keeping them separate was the mistake: the
model runs as its own `llama-server` process with its own lifecycle, so it can be down while
this service is perfectly fine - and when that happened every chat turn fell back to
deterministic text that reads like a working conversation. Nobody could tell from the outside.

`status` is "degraded" when either dependency is unavailable, so one line answers "is the study
stack actually working".
"""
import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..schemas import HealthResponse

router = APIRouter()

#: Short. This endpoint is polled, and a hanging health check is worse than a negative one.
MODEL_PROBE_TIMEOUT_SECONDS = 3.0


async def probe_model() -> str:
    """Whether inference is reachable. Never raises - a health check must always answer."""
    settings = get_settings()
    if not settings.llama_server_url:
        return "not_configured"
    try:
        async with httpx.AsyncClient(timeout=MODEL_PROBE_TIMEOUT_SECONDS) as client:
            response = await client.get(settings.llama_server_url.rstrip("/") + "/health")
    except httpx.HTTPError:
        return "unreachable"
    if response.status_code == 200:
        return "ok"
    # llama-server answers 503 with {"status":"loading model"} while the weights are being read,
    # which is a real state worth distinguishing from a process that is not running at all.
    return "loading" if response.status_code == 503 else "degraded"


@router.get("/health", response_model=HealthResponse)
async def health(db: Session = Depends(get_db)) -> HealthResponse:
    try:
        db.execute(text("SELECT 1"))
        database = "ok"
    except Exception:
        database = "unavailable"

    model = await probe_model()

    # "not_configured" is not a fault: spec 04 freeze item 2 requires this service to be useful
    # with no inference server at all, and the app treats an absent model as fallback-only.
    healthy = database == "ok" and model in ("ok", "not_configured")
    return HealthResponse(
        status="ok" if healthy else "degraded",
        database=database,
        model=model,
    )
