"""API router definitions for ShadowHeal."""

from datetime import datetime, timezone
import json
import logging
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError

from backend.app.config import Settings, get_settings
from backend.app.core.redis import get_redis
from backend.app.schemas.s import EventIngestRequest, EventIngestResponse

logger = logging.getLogger(__name__)

api_router = APIRouter()


@api_router.get(
    "/health",
    status_code=status.HTTP_200_OK,
    summary="Liveness Health Check",
    description="Liveness probe confirming the FastAPI application process is running.",
)
async def health_check() -> dict[str, Any]:
    """Return application process liveness status."""
    return {
        "status": "ok",
        "service": "shadowheal-api",
    }


@api_router.get(
    "/ready",
    summary="Readiness Probe",
    description="Readiness check verifying connectivity to Redis queue.",
    responses={
        status.HTTP_200_OK: {
            "description": "Redis connection active and responsive",
            "content": {"application/json": {"example": {"status": "ready", "redis": "connected"}}},
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Redis connection unavailable",
            "content": {"application/json": {"example": {"status": "not_ready", "redis": "unavailable"}}},
        },
    },
)
async def readiness_check(request: Request) -> JSONResponse:
    """Return application readiness status based on Redis connectivity."""
    redis_client = getattr(request.app.state, "redis", None)
    if redis_client is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "not_ready",
                "redis": "unavailable",
            },
        )

    try:
        is_connected = await redis_client.ping()
        if is_connected:
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "status": "ready",
                    "redis": "connected",
                },
            )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "not_ready",
                "redis": "unavailable",
            },
        )
    except (RedisError, OSError, TimeoutError):
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "not_ready",
                "redis": "unavailable",
            },
        )


@api_router.post(
    "/v1/events/ingest",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=EventIngestResponse,
    summary="Ingest Error Telemetry Event",
    description=(
        "Receive and validate runtime error events dispatched from the ShadowHeal SDK. "
        "Upon successful validation, enqueues the event into Redis via LPUSH and acknowledges "
        "with HTTP 202 Accepted. Downstream workers consume via RPOP or BRPOP for FIFO order."
    ),
    responses={
        status.HTTP_202_ACCEPTED: {
            "description": "Event validated and accepted into Redis ingestion queue",
            "model": EventIngestResponse,
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "Validation error: malformed or invalid error payload",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Infrastructure error: Redis queue service is unavailable",
        },
    },
)
async def ingest_event(
    payload: EventIngestRequest,
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> EventIngestResponse:
    """Validate, structure, and enqueue an incoming error event into Redis."""
    event_id = uuid4()
    queued_at = datetime.now(timezone.utc)

    response = EventIngestResponse(
        status="accepted",
        event_id=event_id,
        fingerprint=payload.fingerprint,
        queued_at=queued_at,
    )

    stored_event = {
        "event_id": str(event_id),
        "fingerprint": payload.fingerprint,
        "error": payload.error.model_dump(mode="json"),
        "queued_at": response.model_dump(mode="json")["queued_at"],
    }

    try:
        event_json = json.dumps(stored_event, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        logger.error("Payload serialization failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Payload serialization failed: invalid non-standard JSON values",
        ) from exc

    try:
        await redis.lpush(settings.REDIS_QUEUE_NAME, event_json)
    except (RedisError, OSError, TimeoutError) as exc:
        logger.error("Failed to enqueue event into Redis: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to queue event: Redis service unavailable",
        ) from exc

    return response
