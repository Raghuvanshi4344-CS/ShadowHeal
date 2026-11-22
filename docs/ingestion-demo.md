# ShadowHeal Layer 2 Ingestion — Capstone Demonstration Guide

This guide provides reproducible, step-by-step terminal instructions for demonstrating the **Layer 2 Telemetry Ingestion Gateway** to faculty, advisors, and team members.

---

## Scope & Disclaimers

> [!IMPORTANT]
> - **Demonstrated Capability:** Ingestion of telemetry errors, schema validation, asynchronous Redis queue buffering, health/readiness monitoring, and SDK integration with in-memory duplicate suppression.
> - **Queue Acceptance Semantics:** An HTTP 202 response indicates **queue acceptance only** into Redis memory. It does not indicate completed diagnosis or permanent database persistence.
> - **Out of Scope / Future Work:** Multi-agent AI diagnosis (Layer 4), downstream background workers (Layer 3), API key authentication, and sliding-window rate limiting.

---

## Prerequisites

1. Active Python virtual environment:
   ```bash
   source .venv/bin/activate
   ```
2. Docker daemon running locally.
3. Terminal positioned at the project root (`/home/rai/Projects/ShadowHeal`).

---

## Step-by-Step Demonstration

### Step 1: Start Redis Infrastructure
Launch the standalone Redis service inside Docker:
```bash
docker compose up -d redis
```
Verify container status:
```bash
docker compose ps redis
```
**Expected Output:**
`shadowheal-redis ... Up ... 127.0.0.1:6379->6379/tcp`

*What this proves:* Isolates the caching layer from database dependencies and binds Redis strictly to local loopback (`127.0.0.1:6379`) for security.

---

### Step 2: Start the FastAPI Ingestion Server
Launch the FastAPI development server using Uvicorn:
```bash
PYTHONPATH=. .venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```
*(Run in background or in a separate terminal window)*

**Expected Output:**
```
INFO:     Started server process [...]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

*What this proves:* Initializes the FastAPI application, executes the async lifespan context manager, and connects the shared async Redis connection pool.

---

### Step 3: Check Liveness and Readiness Probes

#### 3A. Liveness Probe (`GET /health`)
```bash
curl -s -i http://127.0.0.1:8000/health
```
**Expected Output:**
```http
HTTP/1.1 200 OK
content-type: application/json

{"status":"ok","service":"shadowheal-api"}
```
*What this proves:* The web server process is responsive and serving traffic independently of downstream services.

#### 3B. Readiness Probe (`GET /ready`)
```bash
curl -s -i http://127.0.0.1:8000/ready
```
**Expected Output:**
```http
HTTP/1.1 200 OK
content-type: application/json

{"status":"ready","redis":"connected"}
```
*What this proves:* The application can communicate with Redis via the shared connection pool and ping check.

---

### Step 4: Explore Interactive Documentation (Swagger UI)
Open a web browser or curl the OpenAPI schema endpoint:
- Interactive UI: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- OpenAPI Specification:
```bash
curl -s http://127.0.0.1:8000/openapi.json | jq .info
```
**Expected Output:**
```json
{
  "title": "ShadowHeal API",
  "description": "ShadowHeal Ingestion Gateway and Observability API. Receives diagnostic runtime events from client SDKs and queues them for automated fault diagnosis and memory-assisted repair.",
  "version": "0.1.0"
}
```
*What this proves:* FastAPI auto-generates schema documentation reflecting `EventIngestRequest` and `EventIngestResponse` Pydantic contracts.

---

### Step 5: Send a Valid Error Report
Dispatch a valid diagnostic event matching the SDK contract:
```bash
curl -s -X POST http://127.0.0.1:8000/v1/events/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "fingerprint": "7537b8ff7980b67e7da201889c258d4a96096515c0e0b3c66286bb9f33b1e326",
    "error": {
      "type": "ZeroDivisionError",
      "message": "division by zero",
      "traceback": "Traceback (most recent call last):\n  File \"app.py\", line 12",
      "timestamp": "2026-11-23T01:00:00Z",
      "context": {"user_id": 104}
    }
  }' | jq .
```
**Expected Output:**
```json
{
  "status": "accepted",
  "event_id": "<generated-uuid4>",
  "fingerprint": "7537b8ff7980b67e7da201889c258d4a96096515c0e0b3c66286bb9f33b1e326",
  "queued_at": "2026-11-23T01:00:00.000000Z"
}
```
*What this proves:* The ingestion route validates the input, generates a unique UUID, performs an asynchronous `LPUSH` into Redis, and returns `HTTP 202 Accepted` immediately without blocking.

---

### Step 6: Inspect the Redis Queue Entry
Inspect the newly enqueued event at the head of the Redis list:
```bash
docker compose exec redis redis-cli lindex shadowheal:events:raw 0 | jq .
```
**Expected Output:**
```json
{
  "event_id": "<generated-uuid4>",
  "fingerprint": "7537b8ff7980b67e7da201889c258d4a96096515c0e0b3c66286bb9f33b1e326",
  "error": {
    "type": "ZeroDivisionError",
    "message": "division by zero",
    "traceback": "Traceback (most recent call last):\n  File \"app.py\", line 12",
    "timestamp": "2026-11-23T01:00:00Z",
    "context": {
      "user_id": 104
    }
  },
  "queued_at": "2026-11-23T01:00:00.000000Z"
}
```
*What this proves:* Confirms data fidelity in Redis storage. The event retains its exact schema, nested context, and timestamp metadata. Downstream consumers will read this using `RPOP` or `BRPOP` for FIFO ordering.

---

### Step 7: Demonstrate Invalid Payload Rejection (Validation Hardening)

#### 7A. Invalid Fingerprint (Not 64-char hex)
```bash
curl -s -i -X POST http://127.0.0.1:8000/v1/events/ingest \
  -H "Content-Type: application/json" \
  -d '{"fingerprint": "invalid_fingerprint", "error": {"type": "ValueError", "message": "fail"}}'
```
**Expected Output:**
```http
HTTP/1.1 422 Unprocessable Entity
```
```json
{
  "detail": [
    {
      "loc": ["body", "fingerprint"],
      "msg": "String should match pattern '^[0-9a-f]{64}$'",
      "type": "string_pattern_mismatch"
    }
  ]
}
```

#### 7B. Whitespace-Only Error Type
```bash
curl -s -i -X POST http://127.0.0.1:8000/v1/events/ingest \
  -H "Content-Type: application/json" \
  -d '{"fingerprint": "7537b8ff7980b67e7da201889c258d4a96096515c0e0b3c66286bb9f33b1e326", "error": {"type": "   ", "message": "fail"}}'
```
**Expected Output:**
`HTTP/1.1 422 Unprocessable Entity` ("String should have at least 1 character after strip")

#### 7C. Non-Finite Number (NaN / Infinity)
```bash
curl -s -i -X POST http://127.0.0.1:8000/v1/events/ingest \
  -H "Content-Type: application/json" \
  -d '{"fingerprint": "7537b8ff7980b67e7da201889c258d4a96096515c0e0b3c66286bb9f33b1e326", "error": {"type": "ValueError", "message": "fail", "context": {"val": NaN}}}'
```
**Expected Output:**
`HTTP/1.1 422 Unprocessable Entity`

*What this proves:* Malformed requests are rejected at the edge before consuming Redis resources, preventing cache corruption or unhandled deserialization crashes.

---

### Step 8 & 9: Run SDK Demo & Duplicate Suppression
Execute the real SDK demonstration script that fires a repeated error 500 times in a tight loop:
```bash
PYTHONPATH=.:sdk .venv/bin/python scripts/demo_ingestion.py
```
**Expected Output:**
```
=================================================================
 ShadowHeal: Layer 1 SDK -> Layer 2 Ingestion Demonstration
 Target Ingestion Gateway: http://127.0.0.1:8000/v1/events/ingest
=================================================================

1. Initializing SDK ErrorReporter with background daemon worker...
2. Simulating application execution and triggering ZeroDivisionError...
   [Captured Error] Type: ZeroDivisionError
   [Captured Error] Message: division by zero
   [Captured Error] Timestamp: 2026-11-22T19:59:03.029331+00:00

3. Testing error dispatch and in-memory duplicate suppression (500 iterations)...
   - Total Loop Iterations: 500
   - Accepted for Dispatch: 1 (Expected: 1)
   - Suppressed by Cache:   499 (Expected: 499)
   - Client Loop Latency:   5.04 ms (non-blocking)

4. Flushing queued items and shutting down SDK worker thread...
[SUCCESS] Event dispatched and successfully accepted by the ingestion gateway.
```
*What this proves:*
- Real end-to-end communication from the ShadowHeal SDK to the FastAPI backend and Redis queue.
- SDK duplicate suppression (`DuplicateErrorCache`) successfully buffers and drops 499 redundant errors, protecting the ingestion gateway from denial-of-service during cascading failures.

---

### Step 10: Run the Automated Test Suite

The test suite comprises **55 unique tests** across the repository (40 backend unit tests, 8 integration tests, and 7 SDK unit tests).

#### 10A. Run Unit Tests (Isolated from Redis):
```bash
PYTHONPATH=.:sdk .venv/bin/pytest -m unit -v
```
**Result:** 40 unit tests pass (isolated from Redis and Docker).

#### 10B. Run Integration & E2E Tests (Against Docker Redis):
```bash
PYTHONPATH=.:sdk .venv/bin/pytest -m integration -v
```
**Result:** 8 integration tests pass (including 50-request concurrent bursts, FIFO verification, and full live SDK dispatch).

#### 10C. Run Full Regression Suite:
```bash
PYTHONPATH=.:sdk .venv/bin/pytest -v
```
**Result:** 55 unique tests passed (40 unit + 8 integration + 7 SDK tests; no double-counting).

#### 10D. Run Legacy SDK Unit Tests:
```bash
PYTHONPATH=sdk .venv/bin/python -m unittest discover -s sdk/tests -v
```
**Result:** 7 SDK unit tests passed.

*What this proves:* Zero regressions across the entire codebase.

---

### Step 11: Demonstrate Redis Failure and Graceful Auto-Recovery

#### 11A. Simulate Redis Outage
Stop the Redis container:
```bash
docker compose stop redis
```

Check Readiness Probe:
```bash
curl -s -i http://127.0.0.1:8000/ready
```
**Expected Output:**
```http
HTTP/1.1 503 Service Unavailable
content-type: application/json

{"status":"not_ready","redis":"unavailable"}
```

Attempt Ingestion:
```bash
curl -s -i -X POST http://127.0.0.1:8000/v1/events/ingest \
  -H "Content-Type: application/json" \
  -d '{"fingerprint": "7537b8ff7980b67e7da201889c258d4a96096515c0e0b3c66286bb9f33b1e326", "error": {"type": "ValueError", "message": "fail"}}'
```
**Expected Output:**
```http
HTTP/1.1 503 Service Unavailable
content-type: application/json

{"detail":"Failed to queue event: Redis service unavailable"}
```

#### 11B. Demonstrate Automatic Recovery
Restart the Redis container without restarting FastAPI:
```bash
docker compose start redis
sleep 2
```

Re-check Readiness Probe:
```bash
curl -s -i http://127.0.0.1:8000/ready
```
**Expected Output:**
```http
HTTP/1.1 200 OK

{"status":"ready","redis":"connected"}
```
*What this proves:* FastAPI remains running and healthy during infrastructure downtime; as soon as Redis recovers, connection pooling resumes traffic automatically without process restarts.
