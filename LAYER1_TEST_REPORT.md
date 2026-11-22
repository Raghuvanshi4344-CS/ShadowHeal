# ShadowHeal Layer 1 Test Report

## Scope

This report covers only the ShadowHeal Python SDK's fingerprinting, local cache,
background transport, and exception-hook integration.

No FastAPI backend, Redis, worker, diagnosis engine, dashboard, database, or
frontend implementation was changed.

## Files Inspected

- `sdk/shadowheal_sdk/fingerprint.py`
- `sdk/shadowheal_sdk/cache.py`
- `sdk/shadowheal_sdk/reporter.py`
- `sdk/shadowheal_sdk/__init__.py`
- `sdk/tests/test_sdk.py`
- `sdk/README.md`

## Functions Tested

- `fingerprint_error`
- `DuplicateErrorCache.add_if_new`
- `DuplicateErrorCache.discard`
- `DuplicateErrorCache.__len__`
- `ErrorReporter.report`
- `ErrorReporter.close`
- `ErrorReporter._run`
- `ErrorReporter._send`
- `ErrorReporter._notify_error`
- `capture_exception`
- `install_excepthook`

## Test Execution

Command:

```powershell
Set-Location 'C:\Users\priya\Documents\Coding\Project\CapStone'
$env:PYTHONPATH='sdk'
python -m unittest discover -s sdk/tests -p 'test_*.py' -v
```

Result: 24 tests executed, 24 passed.

Actual 500-error E2E terminal line:

```text
500-error duplicate test: dispatched=1, suppressed=499, outbound_requests=2
```

The two outbound requests are intentional: one is the identical-error event and
one is the separate different-error event.

## Test-Results Table

| Test | Expected Result | Actual Result | Status |
|---|---|---|---|
| SHA-256 consistency | Deterministic fingerprint | Mapping order and 100 repeated executions were deterministic | PASS |
| SHA-256 format | 64 hex characters | Regex matched for all tested values | PASS |
| Exception type and stack | Both contribute to signature | Type and stack changes produced different normal-case fingerprints | PASS |
| Empty/unusual messages | Fingerprint generated safely | Empty, Unicode, null, and object values produced 64-character digests | PASS |
| Single error | 1 dispatch | 1 cache acceptance and 1 HTTP dispatch | PASS |
| 500 identical errors | 1 dispatch, 499 suppressed | 1 dispatched, 499 suppressed | PASS |
| Different errors | Separate unique dispatches | 2 unique cache entries; 2 E2E HTTP requests | PASS |
| Cache eviction | Oldest entry evicted at size limit | FIFO eviction passed at `max_entries=2` | PASS |
| Concurrent duplicates | No duplicate dispatch | 100 threads produced 1 accepted report | PASS |
| Daemon transport | Background execution | Worker thread dispatches outside the report caller | PASS |
| HTTP payload | JSON contains fingerprint and error | Configured endpoint, POST method, fingerprint, and error payload verified | PASS |
| Network failure | No host application crash | OSError and HTTPError reached error handler without caller failure | PASS |
| Thread handler failure | Worker remains usable | Handler exception was isolated and subsequent queue delivery worked | PASS |
| Exception interception | Correct interception and original-hook behavior | `capture_exception` extracted type, message, and stack; previous hook was called | PASS |

## Bugs Discovered

1. **Missing global exception-hook integration**
   - The SDK provided primitives but had no `sys.excepthook` integration and no
     traceback payload extraction.
   - This was in `sdk/shadowheal_sdk/__init__.py`, which previously exported only
     cache, fingerprint, and reporter classes.

2. **Send worker could be killed by serialization or unexpected exceptions**
   - `ErrorReporter._send` used `json.dumps` without `default=str`.
   - Unexpected exception types could escape `_send` and stop the worker.
   - A raised custom `error_handler` could also escape and stop the worker.

3. **Close could wait inside `queue.join()` and timeout did not bound sentinel queueing**
   - `ErrorReporter.close` could wait for queue processing before inserting the
     sentinel and did not fully bound its blocking behavior.

## Bugs Fixed

1. Added `capture_exception` and `install_excepthook` in
   `sdk/shadowheal_sdk/__init__.py`.
2. Updated `ErrorReporter._send` to serialize unusual payload values with
   `default=str` and catch unexpected exceptions.
3. Protected `_notify_error` so a broken error handler cannot terminate the
   daemon worker.
4. Updated `ErrorReporter.close` to enqueue the sentinel directly with the
   configured close timeout and preserve the daemon worker's bounded lifecycle.

## Actual Duplicate Suppression Count

- Identical events: 500
- Accepted/dispatch eligible: 1
- Suppressed duplicates: 499

## Actual Outbound HTTP Request Count

- Identical error test: 1 request
- Separate different error: 1 request
- Total E2E count: 2 requests

## Thread Execution Verification

- `ErrorReporter.report` enqueues and returns without waiting for `urlopen`.
- The worker runs in a named, daemon thread:
  `shadowheal-error-reporter`.
- Mocked HTTP tests verified background delivery and network error handling.
- Local HTTP server test verified queued JSON POST delivery.

## Remaining Limitations

- The cache has a maximum size (`10,000` entries by default) and FIFO eviction,
  but no TTL expiration. A fingerprint can remain stored until evicted.
- Delivery is best effort. The in-memory queue is not durable.
- A process exit or application shutdown before the daemon finishes can lose
  queued events. `close()` should be used for explicit shutdown.
- `close(timeout=...)` bounds thread joining and sentinel queueing, but it cannot
  guarantee network delivery if `urlopen` ignores transport timeout behavior or
  the process exits.
- HTTP 3xx/4xx/5xx handling depends on `urllib` raising for non-success HTTP
  responses; exceptions are passed to the configured error handler.
- No automatic retry, dead-letter queue, or delivery acknowledgement is present.
- There is no hash-collision guarantee for theoretical SHA-256 input collisions;
  tests verify deterministic behavior and normal distinct-signature cases.

## Modified Files

- `sdk/shadowheal_sdk/__init__.py`
- `sdk/shadowheal_sdk/reporter.py`
- `sdk/tests/test_sdk.py`
- `sdk/README.md`
- `LAYER1_TEST_REPORT.md`
