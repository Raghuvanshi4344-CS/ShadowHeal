"""Tests for asynchronous Redis integration and readiness endpoint (SH-R05)."""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError, RedisError, TimeoutError

from backend.app.config import Settings
from backend.app.core.redis import (
    close_redis_client,
    create_redis_pool,
    get_redis,
    init_redis_client,
)
from backend.app.main import app


@pytest.mark.unit
def test_redis_client_initialization():
    """Verify Redis pool and client are created with configured settings and timeouts."""
    settings = Settings(
        REDIS_HOST="127.0.0.1",
        REDIS_PORT=6379,
        REDIS_QUEUE_NAME="shadowheal:events:raw",
    )
    pool = create_redis_pool(settings)
    assert pool.connection_kwargs["host"] == "127.0.0.1"
    assert pool.connection_kwargs["port"] == 6379
    assert pool.connection_kwargs["socket_connect_timeout"] == 2.0
    assert pool.connection_kwargs["socket_timeout"] == 2.0

    client = init_redis_client(pool=pool)
    assert isinstance(client, Redis)


@pytest.mark.unit
def test_readiness_success_with_mock():
    """Verify /ready returns HTTP 200 when Redis ping succeeds."""
    with TestClient(app) as client:
        mock_redis = AsyncMock()
        mock_redis.ping.return_value = True
        app.state.redis = mock_redis

        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json() == {
            "status": "ready",
            "redis": "connected",
        }
        mock_redis.ping.assert_awaited_once()


@pytest.mark.unit
def test_readiness_failure_on_redis_connection_error():
    """Verify /ready returns HTTP 503 and safe payload when Redis is unreachable."""
    with TestClient(app) as client:
        mock_redis = AsyncMock()
        mock_redis.ping.side_effect = ConnectionError("Connection refused")
        app.state.redis = mock_redis

        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json() == {
            "status": "not_ready",
            "redis": "unavailable",
        }


@pytest.mark.unit
def test_readiness_failure_on_redis_timeout_error():
    """Verify /ready returns HTTP 503 when Redis operation times out."""
    with TestClient(app) as client:
        mock_redis = AsyncMock()
        mock_redis.ping.side_effect = TimeoutError("Timed out")
        app.state.redis = mock_redis

        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json() == {
            "status": "not_ready",
            "redis": "unavailable",
        }


@pytest.mark.unit
def test_readiness_failure_when_client_none():
    """Verify /ready returns HTTP 503 when Redis client is missing from state."""
    with TestClient(app) as client:
        app.state.redis = None
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json() == {
            "status": "not_ready",
            "redis": "unavailable",
        }


@pytest.mark.unit
def test_liveness_unaffected_by_redis_failure():
    """Verify /health (liveness) continues returning 200 even when Redis is down."""
    with TestClient(app) as client:
        mock_redis = AsyncMock()
        mock_redis.ping.side_effect = ConnectionError("Redis is completely down")
        app.state.redis = mock_redis

        # Readiness fails
        ready_res = client.get("/ready")
        assert ready_res.status_code == 503

        # Liveness still succeeds
        health_res = client.get("/health")
        assert health_res.status_code == 200
        assert health_res.json() == {
            "status": "ok",
            "service": "shadowheal-api",
        }


@pytest.mark.unit
def test_redis_resource_cleanup():
    """Verify close_redis_client awaits aclose with close_connection_pool=True."""
    import asyncio

    mock_redis = AsyncMock()
    asyncio.run(close_redis_client(mock_redis))
    mock_redis.aclose.assert_awaited_once_with(close_connection_pool=True)

    # Verify None is handled gracefully without error
    asyncio.run(close_redis_client(None))


@pytest.mark.unit
def test_get_redis_dependency():
    """Verify get_redis dependency extracts client from request.app.state or raises HTTP 503."""
    import asyncio
    from fastapi import HTTPException

    mock_request = AsyncMock()
    mock_client = AsyncMock(spec=Redis)
    mock_request.app.state.redis = mock_client

    retrieved = asyncio.run(get_redis(mock_request))
    assert retrieved is mock_client

    # When missing, raises HTTP 503 HTTPException
    mock_request.app.state.redis = None
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(get_redis(mock_request))
    assert exc_info.value.status_code == 503


@pytest.mark.integration
def test_real_redis_readiness_integration():
    """Integration test verifying /ready succeeds against the live Redis container."""
    with TestClient(app) as client:
        # Default lifespan connects to the local running Redis container
        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json() == {
            "status": "ready",
            "redis": "connected",
        }
