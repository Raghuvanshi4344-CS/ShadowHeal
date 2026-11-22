"""Asynchronous Redis client and connection pool management."""

import logging
from typing import cast

from fastapi import HTTPException, Request, status
from redis.asyncio import ConnectionPool, Redis
from redis.exceptions import RedisError

from backend.app.config import Settings, get_settings

logger = logging.getLogger(__name__)


def create_redis_pool(settings: Settings | None = None) -> ConnectionPool:
    """Create a shared asynchronous Redis connection pool with bounded timeouts."""
    cfg = settings or get_settings()
    return ConnectionPool(
        host=cfg.REDIS_HOST,
        port=cfg.REDIS_PORT,
        decode_responses=True,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )


def init_redis_client(pool: ConnectionPool | None = None, settings: Settings | None = None) -> Redis:
    """Initialize an asynchronous Redis client backed by a connection pool."""
    connection_pool = pool or create_redis_pool(settings)
    return Redis(connection_pool=connection_pool)


async def close_redis_client(redis_client: Redis | None) -> None:
    """Gracefully close the Redis client and release its connection pool resources."""
    if redis_client is not None:
        try:
            await redis_client.aclose(close_connection_pool=True)
        except Exception as exc:
            logger.warning("Error closing Redis client: %s", exc)


async def get_redis(request: Request) -> Redis:
    """FastAPI dependency to retrieve the shared Redis client from app.state.

    Raises a controlled HTTP 503 if the client is missing or uninitialized.
    """
    redis_client = getattr(request.app.state, "redis", None)
    if redis_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to queue event: Redis service unavailable",
        )
    return cast(Redis, redis_client)
