import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import get_settings, verify_deployment_is_safe
from .routes.auth import router as auth_router
from .routes.backup import router as backup_router
from .routes.dashboard import router as dashboard_router
from .routes.generate import router as generate_router
from .routes.health import router as health_router
from .routes.participants import router as participants_router
from .routes.research import router as research_router
from .routes.sync import router as sync_router

# Before anything is served, and deliberately at import: a misconfigured deployment must fail
# to start rather than come up looking healthy with its authentication switched off.
verify_deployment_is_safe(get_settings())

logger = logging.getLogger("quitsmoke.startup")


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Says on the way up whether inference is actually reachable.

    Deliberately does **not** refuse to start when the model is down. Sync, backup and export
    need no model, and taking the whole service down would turn one missing GPU process into an
    outage for research collection - which spec 04 freeze item 2 rules out. It logs loudly and
    `/health` reports `degraded`, so the state is visible instead of silent.

    The silence was the actual problem: a failed model task left this service looking perfectly
    healthy while every chat turn fell back to deterministic text, and the only symptom was
    replies that read slightly flat.
    """
    from .routes.health import probe_model

    settings = get_settings()
    if not settings.llama_server_url:
        logger.warning(
            "No LLAMA_SERVER_URL configured - every chat turn will use the deterministic "
            "fallback. That is a supported mode, not a fault."
        )
    else:
        state = await probe_model()
        if state == "ok":
            logger.info("Model reachable at %s", settings.llama_server_url)
        else:
            logger.error(
                "MODEL %s at %s - chat turns will fall back to deterministic text until it is "
                "running. Start it with: Tasks: Run Task -> 2. Model (llama-server)",
                state.upper(),
                settings.llama_server_url,
            )
    yield


app = FastAPI(title="Quit Smoke Research Backend", version="1.0.0", lifespan=lifespan)
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(participants_router)
app.include_router(sync_router)
app.include_router(generate_router)
app.include_router(backup_router)
app.include_router(research_router)
app.include_router(dashboard_router)
