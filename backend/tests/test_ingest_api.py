"""Tests for ShadowHeal event ingestion endpoint POST /v1/events/ingest (SH-R06)."""

from datetime import datetime, timezone
import json
from unittest.mock import AsyncMock
from uuid import UUID

from fastapi.testclient import TestClient
import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError, TimeoutError

from backend.app.config import Settings
from backend.app.main import app
from shadowheal_sdk import fingerprint_error

pytestmark = pytest.mark.unit


def test_01_to_05_valid_payload_enqueues_and_returns_202():
    """Verify requirements 1-5:
    1. Valid payload returns HTTP 202.
    2. Response includes event_id, fingerprint, and timezone-aware timestamp.
    3. Redis LPUSH is called exactly once.
    4. Queued JSON contains correct event data.
    5. Optional error metadata is preserved.
    """
    raw_error = {
        "type": "ZeroDivisionError",
        "message": "division by zero",
        "traceback": "Traceback (most recent call last):\n  File 'app.py', line 10",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "context": {"user": "tester", "attempt": 1},
        "custom_sdk_field": "v1.0.0",
    }
    fp = fingerprint_error(raw_error)

    payload = {
        "fingerprint": fp,
        "error": raw_error,
    }

    with TestClient(app) as client:
        mock_redis = AsyncMock(spec=Redis)
        mock_redis.lpush = AsyncMock(return_value=1)
        app.state.redis = mock_redis

        response = client.post("/v1/events/ingest", json=payload)

        # 1. HTTP 202
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"

        # 2. Response fields
        event_id_str = data["event_id"]
        UUID(event_id_str)  # Validates UUID format
        assert data["fingerprint"] == fp
        queued_at_dt = datetime.fromisoformat(data["queued_at"])
        assert queued_at_dt.tzinfo is not None

        # 3. LPUSH called exactly once
        mock_redis.lpush.assert_awaited_once()
        call_args = mock_redis.lpush.call_args[0]
        queue_name, queued_json_str = call_args[0], call_args[1]

        # 4. Queued JSON verification
        assert queue_name == app.state.settings.REDIS_QUEUE_NAME
        queued_obj = json.loads(queued_json_str)
        assert queued_obj["event_id"] == event_id_str
        assert queued_obj["fingerprint"] == fp
        assert queued_obj["queued_at"] == data["queued_at"]

        # 5. Optional error metadata preservation
        queued_error = queued_obj["error"]
        assert queued_error["type"] == "ZeroDivisionError"
        assert queued_error["message"] == "division by zero"
        assert queued_error["traceback"] == raw_error["traceback"]
        assert queued_error["context"] == {"user": "tester", "attempt": 1}
        assert queued_error["custom_sdk_field"] == "v1.0.0"


def test_06_invalid_payload_returns_422_without_queueing():
    """Verify requirement 6: Invalid payloads return HTTP 422 and never call LPUSH."""
    with TestClient(app) as client:
        mock_redis = AsyncMock(spec=Redis)
        mock_redis.lpush = AsyncMock()
        app.state.redis = mock_redis

        # Missing fingerprint
        res1 = client.post("/v1/events/ingest", json={"error": {"type": "Error", "message": "msg"}})
        assert res1.status_code == 422

        # Invalid fingerprint format (too short)
        res2 = client.post(
            "/v1/events/ingest",
            json={"fingerprint": "abcd", "error": {"type": "Error", "message": "msg"}},
        )
        assert res2.status_code == 422

        # Missing error object
        res3 = client.post("/v1/events/ingest", json={"fingerprint": "a" * 64})
        assert res3.status_code == 422

        # Disallowed unexpected top-level field
        res4 = client.post(
            "/v1/events/ingest",
            json={
                "fingerprint": "a" * 64,
                "error": {"type": "Error", "message": "msg"},
                "extra": "forbidden",
            },
        )
        assert res4.status_code == 422

        # Verify Redis was never called
        mock_redis.lpush.assert_not_called()


def test_07_redis_connection_failure_returns_503():
    """Verify requirement 7: Redis connection failure produces HTTP 503."""
    raw_error = {"type": "RuntimeError", "message": "server error"}
    payload = {"fingerprint": fingerprint_error(raw_error), "error": raw_error}

    with TestClient(app) as client:
        mock_redis = AsyncMock(spec=Redis)
        mock_redis.lpush = AsyncMock(side_effect=ConnectionError("Could not connect to Redis"))
        app.state.redis = mock_redis

        response = client.post("/v1/events/ingest", json=payload)
        assert response.status_code == 503
        assert "unavailable" in response.text.lower()


def test_08_redis_timeout_returns_503():
    """Verify requirement 8: Redis timeout produces HTTP 503."""
    raw_error = {"type": "TimeoutError", "message": "redis timed out"}
    payload = {"fingerprint": fingerprint_error(raw_error), "error": raw_error}

    with TestClient(app) as client:
        mock_redis = AsyncMock(spec=Redis)
        mock_redis.lpush = AsyncMock(side_effect=TimeoutError("Redis socket timeout"))
        app.state.redis = mock_redis

        response = client.post("/v1/events/ingest", json=payload)
        assert response.status_code == 503
        assert "unavailable" in response.text.lower()


def test_09_missing_redis_client_returns_503():
    """Verify requirement 9: Missing Redis client in state produces HTTP 503."""
    raw_error = {"type": "ValueError", "message": "missing client"}
    payload = {"fingerprint": fingerprint_error(raw_error), "error": raw_error}

    with TestClient(app) as client:
        app.state.redis = None

        response = client.post("/v1/events/ingest", json=payload)
        assert response.status_code == 503


def test_10_repeated_requests_generate_distinct_event_ids():
    """Verify requirement 10: Repeated valid requests generate unique event IDs."""
    raw_error = {"type": "SameError", "message": "happened twice"}
    payload = {"fingerprint": fingerprint_error(raw_error), "error": raw_error}

    with TestClient(app) as client:
        mock_redis = AsyncMock(spec=Redis)
        mock_redis.lpush = AsyncMock(return_value=1)
        app.state.redis = mock_redis

        res1 = client.post("/v1/events/ingest", json=payload)
        res2 = client.post("/v1/events/ingest", json=payload)

        assert res1.status_code == 202
        assert res2.status_code == 202
        id1 = res1.json()["event_id"]
        id2 = res2.json()["event_id"]
        assert id1 != id2
        assert mock_redis.lpush.call_count == 2


def test_11_malformed_json_body_returns_422_without_queueing():
    """Verify malformed JSON syntax in HTTP body returns 422 without queue insertion."""
    with TestClient(app) as client:
        mock_redis = AsyncMock(spec=Redis)
        mock_redis.lpush = AsyncMock()
        app.state.redis = mock_redis

        raw_malformed_body = b'{"fingerprint": "unclosed_json...'
        response = client.post(
            "/v1/events/ingest",
            content=raw_malformed_body,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422
        mock_redis.lpush.assert_not_called()


def test_12_whitespace_type_returns_422_without_queueing():
    """Verify whitespace-only error type returns 422 without queue insertion."""
    with TestClient(app) as client:
        mock_redis = AsyncMock(spec=Redis)
        mock_redis.lpush = AsyncMock()
        app.state.redis = mock_redis

        payload = {
            "fingerprint": "a" * 64,
            "error": {"type": "   ", "message": "whitespace error"},
        }
        response = client.post("/v1/events/ingest", json=payload)
        assert response.status_code == 422
        mock_redis.lpush.assert_not_called()


def test_13_numeric_timestamp_returns_422_without_queueing():
    """Verify numeric unix timestamp returns 422 without queue insertion."""
    with TestClient(app) as client:
        mock_redis = AsyncMock(spec=Redis)
        mock_redis.lpush = AsyncMock()
        app.state.redis = mock_redis

        payload = {
            "fingerprint": "a" * 64,
            "error": {
                "type": "RuntimeError",
                "message": "test msg",
                "timestamp": 1700000000,
            },
        }
        response = client.post("/v1/events/ingest", json=payload)
        assert response.status_code == 422
        mock_redis.lpush.assert_not_called()


def test_14_non_finite_number_returns_422_without_queueing():
    """Verify non-finite numbers (NaN) return 422 without queue insertion."""
    with TestClient(app) as client:
        mock_redis = AsyncMock(spec=Redis)
        mock_redis.lpush = AsyncMock()
        app.state.redis = mock_redis

        # Send raw JSON containing NaN token
        raw_body = (
            b'{"fingerprint": "' + ("a" * 64).encode() + b'", '
            b'"error": {"type": "MathError", "message": "msg", "context": {"score": NaN}}}'
        )
        response = client.post(
            "/v1/events/ingest",
            content=raw_body,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422
        mock_redis.lpush.assert_not_called()


def test_15_openapi_documentation_documents_503_and_422():
    """Verify OpenAPI documentation explicitly documents 202, 422, and 503 for ingest."""
    with TestClient(app) as client:
        response = client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        ingest_op = schema["paths"]["/v1/events/ingest"]["post"]
        responses = ingest_op["responses"]
        assert "202" in responses
        assert "422" in responses
        assert "503" in responses
