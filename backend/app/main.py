"""Main entry point for ShadowHeal FastAPI backend."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from backend.app.api.api import api_router
from backend.app.config import get_settings
from backend.app.core.redis import close_redis_client, init_redis_client


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage application startup and shutdown lifecycle events."""
    settings = get_settings()
    app.state.settings = settings
    app.state.redis = init_redis_client(settings=settings)
    try:
        yield
    finally:
        await close_redis_client(getattr(app.state, "redis", None))


app = FastAPI(
    title="ShadowHeal API",
    version="0.1.0",
    description=(
        "ShadowHeal Ingestion Gateway and Observability API. "
        "Receives diagnostic runtime events from client SDKs and queues them "
        "for automated fault diagnosis and memory-assisted repair."
    ),
    docs_url="/docs",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Handle validation errors without reflecting sensitive input payloads or non-finite numbers."""
    sanitized_errors = [
        {
            "loc": err.get("loc"),
            "msg": err.get("msg"),
            "type": err.get("type"),
        }
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": sanitized_errors},
    )


app.include_router(api_router)
