"""Real End-to-End integration test: ShadowHeal SDK -> Real Uvicorn FastAPI -> Real Redis (SH-R09).

Demonstrates genuine network communication, thread daemon delivery,
local client duplicate suppression, and queue persistence without mocking.
"""

import asyncio
from datetime import datetime, timezone
import json
import socket
import threading
import time
from uuid import uuid4

import pytest
from redis.asyncio import Redis
import uvicorn

from backend.app.main import app
from shadowheal_sdk import ErrorReporter, fingerprint_error

pytestmark = pytest.mark.integration


def get_free_port() -> int:
    """Find a free TCP port dynamically bound to loopback."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_sdk_to_api_to_redis_e2e():
    """Real end-to-end integration test exercising actual HTTP transport and Redis queueing."""
    test_port = get_free_port()
    test_queue = f"shadowheal:test:e2e:{uuid4().hex}"

    # Configure and launch real Uvicorn server in a background thread
    server_config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=test_port,
        log_level="warning",
    )
    server = uvicorn.Server(server_config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    # Wait for Uvicorn server to initialize
    for _ in range(50):
        if server.started and hasattr(app.state, "settings"):
            break
        time.sleep(0.05)

    assert server.started, "Uvicorn server failed to start within timeout"

    original_queue = app.state.settings.REDIS_QUEUE_NAME
    app.state.settings.REDIS_QUEUE_NAME = test_queue

    endpoint_url = f"http://127.0.0.1:{test_port}/v1/events/ingest"
    transport_errors: list[Exception] = []

    try:
        # 1. Initialize real SDK ErrorReporter targeting real local endpoint
        reporter = ErrorReporter(
            endpoint_url=endpoint_url,
            timeout=5.0,
            error_handler=transport_errors.append,
        )

        # 2. Construct first error and report 500 times
        error_1 = {
            "type": "ZeroDivisionError",
            "message": "division by zero in worker_loop",
            "traceback": "Traceback (most recent call last):\n  File 'worker.py', line 12",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "context": {"module": "calculation", "iteration": 42},
            "sdk_client": "python-e2e-test",
        }
        fp_1 = fingerprint_error(error_1)

        accepted_reports = 0
        suppressed_reports = 0
        for _ in range(500):
            if reporter.report(error_1):
                accepted_reports += 1
            else:
                suppressed_reports += 1

        # Verify client-side deduplication logic
        assert accepted_reports == 1
        assert suppressed_reports == 499

        # Gracefully close first reporter (flushes queue over genuine HTTP)
        reporter.close(timeout=5.0)
        assert len(transport_errors) == 0

        # 3. Construct a distinct second error and report via new reporter instance
        error_2 = {
            "type": "KeyError",
            "message": "'missing_config_key'",
            "traceback": "Traceback (most recent call last):\n  File 'config.py', line 8",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "context": {"key": "missing_config_key"},
        }
        fp_2 = fingerprint_error(error_2)
        assert fp_1 != fp_2

        reporter_2 = ErrorReporter(
            endpoint_url=endpoint_url,
            timeout=5.0,
            error_handler=transport_errors.append,
        )
        assert reporter_2.report(error_2) is True
        reporter_2.close(timeout=5.0)
        assert len(transport_errors) == 0

        # 4. Verify live Redis queue contents and ordering
        async def verify_redis():
            live_redis = Redis(host="127.0.0.1", port=6379, decode_responses=True)
            try:
                # Expect exactly 2 events in the queue (1 from 500 identical, 1 from distinct error)
                queue_length = await live_redis.llen(test_queue)
                assert queue_length == 2

                # FIFO check via RPOP: Event 1 should pop first
                raw_item_1 = await live_redis.rpop(test_queue)
                assert raw_item_1 is not None
                event_1 = json.loads(raw_item_1)
                assert event_1["fingerprint"] == fp_1
                assert event_1["error"]["type"] == "ZeroDivisionError"
                assert event_1["error"]["message"] == error_1["message"]
                assert event_1["error"]["sdk_client"] == "python-e2e-test"
                assert event_1["error"]["context"] == error_1["context"]

                # Event 2 should pop second
                raw_item_2 = await live_redis.rpop(test_queue)
                assert raw_item_2 is not None
                event_2 = json.loads(raw_item_2)
                assert event_2["fingerprint"] == fp_2
                assert event_2["error"]["type"] == "KeyError"
                assert event_2["error"]["message"] == error_2["message"]
            finally:
                await live_redis.delete(test_queue)
                await live_redis.aclose()

        asyncio.run(verify_redis())

    finally:
        # Ensure clean server termination and settings restoration
        app.state.settings.REDIS_QUEUE_NAME = original_queue
        server.should_exit = True
        server_thread.join(timeout=3.0)
