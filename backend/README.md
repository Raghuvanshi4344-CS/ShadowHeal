# ShadowHeal Backend — Layer 2 Ingestion Gateway

## 1. Overview and Purpose

The **ShadowHeal Ingestion Gateway** is the Layer 2 ingestion component of the ShadowHeal observability and self-healing platform. Its responsibility is to provide a high-throughput, non-blocking telemetry ingestion gateway that receives diagnostic failure events dispatched by the Layer 1 Python SDK, validates them against strict data schemas, enqueues them into an asynchronous Redis event queue, and immediately returns an acknowledgement to the client application.

> [!IMPORTANT]
> **Scope Distinction:**
> - **Implemented in this module:** FastAPI application foundation, strict Pydantic v2 validation contracts, asynchronous Redis connection pooling, health/readiness endpoints (`/health`, `/ready`), and the event ingestion route (`POST /v1/events/ingest`).
> - **Not yet implemented / Out of scope for this module:** AI multi-agent diagnosis (Layer 4), downstream background workers (Layer 3), API authentication and sliding-window rate limiting (assigned to Rishav & Harshita), and frontend UI.
> - **HTTP 202 Accepted Semantics:** An HTTP 202 response represents **queue acceptance only**—it does not indicate completed diagnosis or permanent database persistence.

---

## 2. Architecture and Event Lifecycle

```
[Client Application]
       │ (Runtime Error Occurs)
       ▼
[Layer 1: ShadowHeal SDK]
       │ 1. Intercepts error / computes deterministic SHA-256 fingerprint
       │ 2. Suppresses in-memory duplicate loops (DuplicateErrorCache)
       │ 3. Background daemon thread dispatches HTTP POST
       ▼
[Layer 2: FastAPI Ingestion API]  (POST /v1/events/ingest)
       │ 4. Validates request body via Pydantic v2 (EventIngestRequest)
       │ 5. Generates unique event UUID and timezone-aware UTC timestamp
       │ 6. Asynchronously executes LPUSH into Redis queue
       ▼
[Layer 3: Redis Queue (shadowheal:events:raw)]
       │ (Event safely buffered in memory)
       ▼
[Layer 2: FastAPI Response]
       │ 7. Returns HTTP 202 Accepted (EventIngestResponse) to client SDK
       ▼
[Downstream Worker Pools] (Pending Future Implementation)
       │ Consumes via RPOP / BRPOP (FIFO ordering)
       ▼
[Multi-Agent Diagnosis Engine]
```

### Event Lifecycle Details:
1. **Client Dispatch:** The SDK formats the failure into a JSON payload containing a 64-character lowercase hexadecimal SHA-256 fingerprint and nested `error` details.
2. **Schema Validation:** FastAPI parses the request against `EventIngestRequest`. Malformed bodies, non-hex fingerprints, whitespace-only error types, numeric timestamps, and non-finite numbers (`NaN`, `Infinity`) are rejected with `HTTP 422 Unprocessable Entity`.
3. **Queue Ingestion:** The gateway generates a unique event UUID and UTC timestamp, serializes the standardized event payload, and issues an asynchronous `LPUSH` into the Redis queue (`shadowheal:events:raw`).
4. **Immediate Acknowledgment:** Once Redis acknowledges the `LPUSH`, FastAPI returns `HTTP 202 Accepted` with the generated `event_id`, matching `fingerprint`, and `queued_at` timestamp.
5. **FIFO Queue Contract:** Because events enter the queue via `LPUSH`, downstream worker pools must consume events from the right side of the list via `RPOP` or `BRPOP`, guaranteeing First-In, First-Out (FIFO) processing order.

---

## 3. Directory Structure

```
backend/
├── app/
│   ├── api/
│   │   └── api.py              # API router: /health, /ready, /v1/events/ingest
│   ├── core/
│   │   ├── c.py                # Core placeholder
│   │   └── redis.py            # Async Redis connection pool and lifespan manager
│   ├── schemas/
│   │   └── s.py                # Pydantic v2 schemas: ErrorDetail, IngestRequest, IngestResponse
│   ├── config.py               # Application settings (Pydantic Settings & .env loading)
│   ├── main.py                 # FastAPI application factory, lifespan, and exception handlers
│   ├── database.py             # Database placeholder (PostgreSQL - future)
│   ├── models/m.py             # ORM models placeholder (future)
│   ├── worker.py               # Worker placeholder (future)
│   ├── ai_engine/              # AI diagnostic engine placeholder (future)
│   └── rag/                    # Vector RAG placeholder (future)
├── tests/
│   ├── test_schemas.py         # Unit tests: Schema validation contracts (unit)
│   ├── test_app.py             # Unit tests: Application foundation, docs & settings (unit)
│   ├── test_redis.py           # Unit & integration tests: Redis connection lifecycle
│   ├── test_ingest_api.py      # Unit tests: Ingestion endpoint with mocked Redis (unit)
│   ├── test_ingest_integration.py # Integration tests: Real Redis, concurrency & FIFO (integration)
│   └── test_sdk_e2e.py         # End-to-end tests: Real SDK -> Real Uvicorn -> Real Redis (integration)
└── README.md                   # This documentation
```

---

## 4. Setup and Configuration

### Prerequisites
- Python 3.12 (tested on 3.12.13)
- Docker & Docker Compose (for local Redis)

### Installation
From the repository root:

```bash
# 1. Create and activate virtual environment
python3.12 -m venv .venv
source .venv/bin/activate

# 2. Upgrade pip and install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 3. Verify installed dependencies
pip check
```

### Environment Variables
Configuration is managed via Pydantic Settings in `backend/app/config.py`. Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Default settings:
```env
# Database Settings (Reserved for Rishav & Harshita auth scope)
POSTGRES_USER=db_user
POSTGRES_PASSWORD=db_pass
POSTGRES_DB=db_name

# Redis Queue Settings
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_QUEUE_NAME=shadowheal:events:raw
```

---

## 5. Running the Infrastructure and Application

### 1. Start Redis
Start the Redis service using Docker Compose (PostgreSQL is not needed for ingestion):

```bash
docker compose up -d redis
```

Verify Redis is responsive:
```bash
docker compose exec redis redis-cli ping
# Expected output: PONG
```

### 2. Start the FastAPI Ingestion Server
Launch the development server bound to loopback:

```bash
PYTHONPATH=. .venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --reload
```

---

## 6. API Endpoints

### 1. `GET /health` (Liveness Probe)
Verifies that the FastAPI process is running. Does not check downstream dependencies.

* **Response (HTTP 200 OK):**
```json
{
  "status": "ok",
  "service": "shadowheal-api"
}
```

### 2. `GET /ready` (Readiness Probe)
Performs an active `PING` against the Redis connection pool to verify ingestion availability.

* **Response (HTTP 200 OK — Ready):**
```json
{
  "status": "ready",
  "redis": "connected"
}
```

* **Response (HTTP 503 Service Unavailable — Not Ready):**
```json
{
  "status": "not_ready",
  "redis": "unavailable"
}
```

### 3. `POST /v1/events/ingest` (Ingestion Route)
Ingests, validates, and queues incoming telemetry error events.

* **Headers:** `Content-Type: application/json`
* **Request Body:**
```json
{
  "fingerprint": "7537b8ff7980b67e7da201889c258d4a96096515c0e0b3c66286bb9f33b1e326",
  "error": {
    "type": "ZeroDivisionError",
    "message": "division by zero",
    "traceback": "Traceback (most recent call last):\n  File 'app.py', line 12",
    "timestamp": "2026-11-23T01:00:00Z",
    "context": {
      "endpoint": "/calculate",
      "user_id": 104
    }
  }
}
```

* **Response (HTTP 202 Accepted):**
```json
{
  "status": "accepted",
  "event_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
  "fingerprint": "7537b8ff7980b67e7da201889c258d4a96096515c0e0b3c66286bb9f33b1e326",
  "queued_at": "2026-11-23T01:00:00.123456Z"
}
```

* **Validation Error (HTTP 422 Unprocessable Entity):**
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

* **Infrastructure Failure (HTTP 503 Service Unavailable):**
```json
{
  "detail": "Failed to queue event: Redis service unavailable"
}
```

### 4. Interactive Documentation
Swagger UI is accessible at:
- **Swagger:** `http://127.0.0.1:8000/docs`
- **OpenAPI Schema:** `http://127.0.0.1:8000/openapi.json`

---

## 7. Redis Queue Storage Contract

Each successfully ingested event is stored in Redis under the list key configured by `REDIS_QUEUE_NAME` (`shadowheal:events:raw`):

```json
{
  "event_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
  "fingerprint": "7537b8ff7980b67e7da201889c258d4a96096515c0e0b3c66286bb9f33b1e326",
  "error": {
    "type": "ZeroDivisionError",
    "message": "division by zero",
    "traceback": "Traceback (most recent call last):\n  File 'app.py', line 12",
    "timestamp": "2026-11-23T01:00:00Z",
    "context": {
      "endpoint": "/calculate",
      "user_id": 104
    }
  },
  "queued_at": "2026-11-23T01:00:00.123456Z"
}
```

### Downstream Consumer Guidelines
- **Enqueue:** API performs `LPUSH shadowheal:events:raw <event_json>`.
- **Dequeue:** Downstream worker pools must consume via `RPOP` or `BRPOP shadowheal:events:raw <timeout>` to maintain strict FIFO processing order.

---

## 8. Testing Strategy

The test suite comprises **55 unique tests** partitioned into 40 backend unit tests, 8 integration tests, and 7 SDK unit tests using custom pytest markers registered in `pytest.ini`.

```bash
# 1. Run isolated unit tests (40 tests, no Redis or Docker required)
PYTHONPATH=.:sdk .venv/bin/pytest -m unit -v

# 2. Run integration tests (8 tests, requires Docker Redis running)
PYTHONPATH=.:sdk .venv/bin/pytest -m integration -v

# 3. Run complete regression test suite (55 unique tests, no double-counting)
PYTHONPATH=.:sdk .venv/bin/pytest -v

# 4. Run existing SDK unit tests directly via standard unittest runner (7 tests)
PYTHONPATH=sdk .venv/bin/python -m unittest discover -s sdk/tests -v
```

### Integration Test Isolation Guarantees:
- Every integration test dynamically creates an isolated queue key: `shadowheal:test:<unique_uuid>`.
- The shared application queue (`shadowheal:events:raw`) is never modified by tests.
- Database flushes (`FLUSHALL` / `FLUSHDB`) are strictly forbidden and never executed.
- All test queue keys are deleted in `try ... finally` blocks even if test assertions fail.

---

## 9. Troubleshooting

| Symptom | Probable Cause | Resolution |
|---|---|---|
| `GET /ready` returns `HTTP 503` | Redis container is stopped or port is blocked | Run `docker compose ps` and `docker compose up -d redis`. Verify connectivity with `docker compose exec redis redis-cli ping`. |
| `POST /v1/events/ingest` returns `HTTP 422` | Invalid payload schema, missing required field, non-hex fingerprint, or numeric timestamp | Check response `detail`. Ensure `fingerprint` is a 64-char lowercase hex SHA-256 string and `timestamp` is a valid ISO 8601 timezone-aware string. |
| `POST /v1/events/ingest` returns `HTTP 503` | Redis connection timed out or queue key holds wrong data type | Check Redis logs (`docker compose logs redis`). If testing locally, ensure the queue key wasn't previously written as a string. |
| `ModuleNotFoundError: No module named 'backend'` | `PYTHONPATH` not set | Prepend `PYTHONPATH=.` when invoking scripts or uvicorn directly from terminal. |

---

## 10. Team Integration Handover

The ingestion gateway provides clean extension points for pending team contributions:

1. **Prashanth (SDK Core & CLI):**
   * *Scope:* Intercept unhandled exceptions using `sys.excepthook` and read AST source frames.
   * *Handover:* Format the extracted failure into the validated dictionary contract (`type`, `message`, `traceback`, `timestamp`, `context`) and pass it to `ErrorReporter.report(error_mapping)`.
2. **Priyanshu (SDK Transport & Caching):**
   * *Scope:* Asynchronous delivery and duplicate caching.
   * *Handover:* Update `ErrorReporter` to accept an optional `api_key` parameter and attach the `X-API-Key` header to outbound HTTP requests once authentication is active.
3. **Rishav & Harshita (API Authentication & Rate Limiting):**
   * *Scope:* API key database validation and Redis sliding-window rate limiting.
   * *Handover:* Implement FastAPI dependencies (`verify_api_key`, `check_rate_limit`) and inject them into `api_router.post("/v1/events/ingest", dependencies=[...])`.
4. **Worker Pool Owners (Processing & Multi-Agent Diagnosis):**
   * *Scope:* Consumer Group A (Aggregation) and Consumer Group C (AI Diagnosis).
   * *Handover:* Consume raw events from Redis using `BRPOP shadowheal:events:raw` in a worker loop, deserialize JSON, and schedule multi-agent diagnosis jobs.
