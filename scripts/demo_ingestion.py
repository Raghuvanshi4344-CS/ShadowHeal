"""Demonstration script: ShadowHeal Python SDK -> FastAPI Ingestion -> Redis Queue.

Demonstrates runtime error capture, local SHA-256 deduplication, and
asynchronous non-blocking delivery to the Layer 2 ingestion API.
"""

from datetime import datetime, timezone
import sys
import time
import traceback

from shadowheal_sdk import ErrorReporter

INGESTION_URL = "http://127.0.0.1:8000/v1/events/ingest"


def main() -> int:
    transport_errors: list[Exception] = []

    def on_transport_error(exc: Exception) -> None:
        transport_errors.append(exc)
        print(f"[SDK Transport Alert] Network delivery error: {exc}", file=sys.stderr)

    print("=================================================================")
    print(" ShadowHeal: Layer 1 SDK -> Layer 2 Ingestion Demonstration")
    print(f" Target Ingestion Gateway: {INGESTION_URL}")
    print("=================================================================\n")

    # 1. Initialize SDK ErrorReporter
    print("1. Initializing SDK ErrorReporter with background daemon worker...")
    reporter = ErrorReporter(
        endpoint_url=INGESTION_URL,
        timeout=5.0,
        error_handler=on_transport_error,
    )

    # 2. Trigger intentional application exception
    print("2. Simulating application execution and triggering ZeroDivisionError...")
    captured_payload = None
    try:
        # Deliberate runtime failure
        _result = 1 / 0
    except ZeroDivisionError as exc:
        captured_payload = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "context": {
                "environment": "development",
                "demo_script": "scripts/demo_ingestion.py",
                "execution_mode": "manual_demonstration",
            },
        }

    assert captured_payload is not None
    print(f"   [Captured Error] Type: {captured_payload['type']}")
    print(f"   [Captured Error] Message: {captured_payload['message']}")
    print(f"   [Captured Error] Timestamp: {captured_payload['timestamp']}")

    # 3. Submit initial error and demonstrate 500x loop duplicate suppression
    print("\n3. Testing error dispatch and in-memory duplicate suppression (500 iterations)...")
    accepted_count = 0
    suppressed_count = 0

    start_time = time.perf_counter()
    for i in range(500):
        is_new = reporter.report(captured_payload)
        if is_new:
            accepted_count += 1
        else:
            suppressed_count += 1
    duration_ms = (time.perf_counter() - start_time) * 1000

    print(f"   - Total Loop Iterations: 500")
    print(f"   - Accepted for Dispatch: {accepted_count} (Expected: 1)")
    print(f"   - Suppressed by Cache:   {suppressed_count} (Expected: 499)")
    print(f"   - Client Loop Latency:   {duration_ms:.2f} ms (non-blocking)\n")

    # 4. Gracefully close reporter to allow daemon worker to complete HTTP delivery
    print("4. Flushing queued items and shutting down SDK worker thread...")
    reporter.close(timeout=5.0)

    # 5. Check delivery status
    if transport_errors:
        print(f"\n[FAILURE] {len(transport_errors)} transport error(s) occurred during delivery:")
        for err in transport_errors:
            print(f"   - {err}", file=sys.stderr)
        return 1

    print("[SUCCESS] Event dispatched and successfully accepted by the ingestion gateway.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
