"""Model inference for the mobile app.

The app used to run Qwen on the phone. Measured on the target device - a Snapdragon 8 Gen 3
with 7.1 GB RAM - only 2.1 GB was free against the ~2.8 GB the 4B needs, so Android evicted the
memory-mapped weights faster than llama.cpp could map them and no reply ever arrived. Inference
moved here.

**A note on spec 23 §7**, which says this service must never run LLM inference: it still does
not. Generation happens in a separate `llama-server` process with its own lifecycle; this module
is a proxy that forwards a prompt and returns the text. The research service remains a data sink
and export tool, and no research data passes through this route.

The other constraint that survives unchanged is spec 10 §41: the model decides nothing. The app's
`ConversationEngine` has already chosen the safety response, the case, the intervention and the
coping option before it calls this, and it sends a finished prompt. This endpoint does not know
what a behavioural case is and must never learn.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from ..config import get_settings
from ..dependencies import rate_limit, require_api_key
from ..schemas import GenerateRequest, GenerateResponse

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key), Depends(rate_limit)])

logger = logging.getLogger("quitsmoke.generate")

#: One retry, on a connection failure or a 503 only.
#:
#: `llama-server` answers 503 while it reads 2.5 GB of weights, and it binds the port before it
#: finishes - so a turn arriving during startup used to be spent on a single 503 and fall back.
#: A short second attempt converts most of those into a real reply.
#:
#: Deliberately **not** applied to a timeout. A 504 means the model took longer than the whole
#: budget already; retrying doubles a wait the participant is sitting through, and the fallback
#: is instant. One retry, not a loop: past two attempts the honest answer is that it is down.
RETRY_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 1.5


@router.post("/generate", response_model=GenerateResponse)
async def generate(request: GenerateRequest) -> GenerateResponse:
    settings = get_settings()

    if not settings.llama_server_url:
        # Not an error. Spec 04 freeze item 2 requires the app to work with no server at all,
        # and the app treats an unavailable model as fallback-only - a real answer, not a
        # failure (spec 10 §24). Saying so plainly is better than a 500 the app would retry.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No inference server configured",
        )

    payload = {
        "prompt": request.prompt,
        "n_predict": request.max_tokens,
        "temperature": request.temperature,
        "top_p": request.top_p,
        "top_k": request.top_k,
        "stop": request.stop_sequences,
        # The app clears context per turn and sends the whole prompt each time, so caching a
        # prefix here would serve a stale conversation to the next participant.
        "cache_prompt": False,
    }

    started = time.monotonic()
    url = settings.llama_server_url.rstrip("/") + "/completion"
    body = None

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        last = attempt == RETRY_ATTEMPTS
        try:
            async with httpx.AsyncClient(timeout=settings.llama_timeout_seconds) as client:
                response = await client.post(url, json=payload)
                if response.status_code == 503 and not last:
                    # Loading its weights. It binds the port before it can serve.
                    logger.info("Model answered 503 (loading); retrying once in %ss", RETRY_DELAY_SECONDS)
                    await asyncio.sleep(RETRY_DELAY_SECONDS)
                    continue
                response.raise_for_status()
                body = response.json()
                break
        except httpx.TimeoutException:
            # Not retried: a timeout means the model already used the whole budget, and trying
            # again doubles a wait the participant is sitting through for no better odds.
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="Model did not answer in time",
            ) from None
        except httpx.HTTPError as error:
            if last:
                logger.error("Model unreachable at %s after %s attempts: %s", url, attempt, error)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Inference server unreachable",
                ) from None
            logger.info("Model unreachable (attempt %s); retrying in %ss", attempt, RETRY_DELAY_SECONDS)
            await asyncio.sleep(RETRY_DELAY_SECONDS)

    if body is None:
        # Every attempt came back 503 without raising - still down, and the app must be told so
        # it can fall back rather than wait.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Inference server unreachable",
        )

    elapsed_ms = int((time.monotonic() - started) * 1000)
    text = (body.get("content") or "").strip()

    return GenerateResponse(
        text=text,
        generation_ms=elapsed_ms,
        token_count=body.get("tokens_predicted"),
        model=body.get("model"),
    )


@router.get("/generate/health")
async def generate_health() -> dict[str, object]:
    """Whether inference is actually available, separately from the database.

    `/health` reports the database, which stays up when the model is down. Conflating them
    would mean an operator sees a green service while every chat turn is falling back.
    """
    settings = get_settings()
    if not settings.llama_server_url:
        return {"model": "not_configured"}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(settings.llama_server_url.rstrip("/") + "/health")
            return {"model": "ok" if response.status_code == 200 else "degraded"}
    except httpx.HTTPError:
        return {"model": "unreachable"}
