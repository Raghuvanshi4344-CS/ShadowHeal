"""Real Redis integration tests for ShadowHeal Ingestion Gateway (SH-R08).

All tests in this module interact with the live Redis service in Docker.
Isolated test queues are used with guaranteed cleanup in try/finally blocks.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from redis.asyncio import Redis

from backend.app.main import app
from shadowheal_sdk import fingerprint_error

pytestmark = pytest.mark.integration


def test_concurrent_ingestion_50_events():
    """Verify 50 concurrent valid requests succeed, generate unique IDs, and are stored intact."""
    test_queue = f"shadowheal:test:concurrent:{uuid4().hex}"

    with TestClient(app) as client:
        original_queue = app.state.settings.REDIS_QUEUE_NAME
        app.state.settings.REDIS_QUEUE_NAME = test_queue

        try:
            total_events = 50

            def send_event(idx: int):
                raw_err = {
                    "type": f"ConcurrentError_{idx}",
                    "message": f"Failure during concurrent task {idx}",
                    "context": {"task_index": idx, "worker": "pool_worker"},
                }
                fp = fingerprint_error(raw_err)
                payload = {"fingerprint": fp, "error": raw_err}
                return client.post("/v1/events/ingest", json=payload)

            with ThreadPoolExecutor(max_workers=10) as executor:
                responses = list(executor.map(send_event, range(total_events)))

            # Verify all 50 returned HTTP 202
            for resp in responses:
                assert resp.status_code == 202
                assert resp.json()["status"] == "accepted"

            # Verify all event IDs are distinct
            event_ids = [resp.json()["event_id"] for resp in responses]
            assert len(set(event_ids)) == total_events

            # Verify Redis queue length and retrieve all events
            async def verify_queue():
                live_redis = Redis(host="127.0.0.1", port=6379, decode_responses=True)
                try:
                    queue_len = await live_redis.llen(test_queue)
                    assert queue_len == total_events

                    popped_events = []
                    for _ in range(total_events):
                        raw_item = await live_redis.rpop(test_queue)
                        assert raw_item is not None
                        popped_events.append(json.loads(raw_item))

                    # Verify all event IDs were present in the queue
                    stored_ids = {item["event_id"] for item in popped_events}
                    assert stored_ids == set(event_ids)
                finally:
                    await live_redis.delete(test_queue)
                    await live_redis.aclose()

            asyncio.run(verify_queue())
        finally:
            app.state.settings.REDIS_QUEUE_NAME = original_queue


def test_fifo_ordering_sequential_submissions():
    """Verify FIFO ordering: events submitted sequentially via LPUSH are retrieved in order via RPOP."""
    test_queue = f"shadowheal:test:fifo:{uuid4().hex}"

    with TestClient(app) as client:
        original_queue = app.state.settings.REDIS_QUEUE_NAME
        app.state.settings.REDIS_QUEUE_NAME = test_queue

        try:
            sequence_length = 5
            submitted_ids = []

            for seq in range(sequence_length):
                raw_err = {"type": "SequenceError", "message": f"Step {seq}"}
                payload = {"fingerprint": fingerprint_error(raw_err), "error": raw_err}
                res = client.post("/v1/events/ingest", json=payload)
                assert res.status_code == 202
                submitted_ids.append(res.json()["event_id"])

            # Verify RPOP yields the events in the exact submission order
            async def verify_fifo():
                live_redis = Redis(host="127.0.0.1", port=6379, decode_responses=True)
                try:
                    retrieved_ids = []
                    for _ in range(sequence_length):
                        raw_item = await live_redis.rpop(test_queue)
                        assert raw_item is not None
                        item = json.loads(raw_item)
                        retrieved_ids.append(item["event_id"])
                    assert retrieved_ids == submitted_ids
                finally:
                    await live_redis.delete(test_queue)
                    await live_redis.aclose()

            asyncio.run(verify_fifo())
        finally:
            app.state.settings.REDIS_QUEUE_NAME = original_queue


def test_unicode_and_nested_metadata_preservation():
    """Verify accurate preservation of Unicode characters, emojis, and deeply nested JSON structures."""
    test_queue = f"shadowheal:test:unicode:{uuid4().hex}"

    with TestClient(app) as client:
        original_queue = app.state.settings.REDIS_QUEUE_NAME
        app.state.settings.REDIS_QUEUE_NAME = test_queue

        try:
            raw_err = {
                "type": "UnicodeCrashError",
                "message": "💥 数据库连接超时 — Ошибка подключения \u2603 \u2764",
                "context": {
                    "system": "日本語環境",
                    "nested": {
                        "level_1": {
                            "level_2": ["α", "β", "γ", 42, True, None],
                        }
                    },
                },
                "sdk_metadata": "🚀 v1.0.0-rc1",
            }
            fp = fingerprint_error(raw_err)
            payload = {"fingerprint": fp, "error": raw_err}

            response = client.post("/v1/events/ingest", json=payload)
            assert response.status_code == 202
            event_id = response.json()["event_id"]

            async def verify_unicode():
                live_redis = Redis(host="127.0.0.1", port=6379, decode_responses=True)
                try:
                    raw_item = await live_redis.rpop(test_queue)
                    assert raw_item is not None
                    item = json.loads(raw_item)

                    assert item["event_id"] == event_id
                    assert item["error"]["message"] == raw_err["message"]
                    assert item["error"]["context"]["system"] == "日本語環境"
                    assert item["error"]["context"]["nested"]["level_1"]["level_2"] == ["α", "β", "γ", 42, True, None]
                    assert item["error"]["sdk_metadata"] == "🚀 v1.0.0-rc1"
                finally:
                    await live_redis.delete(test_queue)
                    await live_redis.aclose()

            asyncio.run(verify_unicode())
        finally:
            app.state.settings.REDIS_QUEUE_NAME = original_queue


def test_redis_wrong_type_queue_failure():
    """Verify LPUSH against a non-list Redis key results in a clean HTTP 503."""
    test_queue = f"shadowheal:test:wrongtype:{uuid4().hex}"

    with TestClient(app) as client:
        original_queue = app.state.settings.REDIS_QUEUE_NAME
        app.state.settings.REDIS_QUEUE_NAME = test_queue

        try:
            # Set key as a string in Redis, triggering WRONGTYPE on LPUSH
            async def setup_wrong_type():
                live_redis = Redis(host="127.0.0.1", port=6379, decode_responses=True)
                try:
                    await live_redis.set(test_queue, "string_value_not_a_list")
                finally:
                    await live_redis.aclose()

            asyncio.run(setup_wrong_type())

            raw_err = {"type": "WrongTypeError", "message": "triggers WRONGTYPE"}
            payload = {"fingerprint": fingerprint_error(raw_err), "error": raw_err}

            response = client.post("/v1/events/ingest", json=payload)
            assert response.status_code == 503
            assert response.json() == {"detail": "Failed to queue event: Redis service unavailable"}

            async def cleanup():
                live_redis = Redis(host="127.0.0.1", port=6379, decode_responses=True)
                try:
                    await live_redis.delete(test_queue)
                finally:
                    await live_redis.aclose()

            asyncio.run(cleanup())
        finally:
            app.state.settings.REDIS_QUEUE_NAME = original_queue


def test_invalid_requests_never_insert_queue_entries():
    """Verify invalid requests (422) never insert entries into the live Redis queue."""
    test_queue = f"shadowheal:test:no_leak:{uuid4().hex}"

    with TestClient(app) as client:
        original_queue = app.state.settings.REDIS_QUEUE_NAME
        app.state.settings.REDIS_QUEUE_NAME = test_queue

        try:
            # 1. Missing fingerprint
            res1 = client.post("/v1/events/ingest", json={"error": {"type": "Err", "message": "msg"}})
            assert res1.status_code == 422

            # 2. Whitespace type
            res2 = client.post(
                "/v1/events/ingest",
                json={"fingerprint": "a" * 64, "error": {"type": "   ", "message": "msg"}},
            )
            assert res2.status_code == 422

            # 3. Numeric string timestamp
            res3 = client.post(
                "/v1/events/ingest",
                json={"fingerprint": "a" * 64, "error": {"type": "Err", "message": "msg", "timestamp": "1700000000"}},
            )
            assert res3.status_code == 422

            # Verify queue in Redis remains completely empty
            async def verify_empty():
                live_redis = Redis(host="127.0.0.1", port=6379, decode_responses=True)
                try:
                    exists = await live_redis.exists(test_queue)
                    assert exists == 0
                finally:
                    await live_redis.aclose()

            asyncio.run(verify_empty())
        finally:
            app.state.settings.REDIS_QUEUE_NAME = original_queue


def test_no_sensitive_payload_reflection_in_validation_response():
    """Verify sensitive customer tokens or keys are never echoed back in 422 responses."""
    with TestClient(app) as client:
        secret_content = "super_secret_api_key_XYZ_999"
        raw_body = (
            b'{"fingerprint": "' + ("a" * 64).encode() + b'", '
            b'"error": {"type": "AuthError", "message": "' + secret_content.encode() + b'", '
            b'"context": {"invalid_float": NaN}}}'
        )
        response = client.post(
            "/v1/events/ingest",
            content=raw_body,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422
        # Verify secret_content is NOT reflected in the response text
        assert secret_content not in response.text
