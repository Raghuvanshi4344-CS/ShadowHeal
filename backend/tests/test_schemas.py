"""Unit tests for ShadowHeal Ingestion API Pydantic schemas (SH-R03)."""

from datetime import datetime, timezone
import json
from uuid import uuid4

from pydantic import ValidationError
import pytest

from backend.app.schemas.s import (
    ErrorDetail,
    EventIngestRequest,
    EventIngestResponse,
)
from shadowheal_sdk import fingerprint_error

pytestmark = pytest.mark.unit


def test_01_valid_sdk_payload():
    """Verify that a standard SDK-generated error payload passes validation."""
    raw_error = {
        "type": "ZeroDivisionError",
        "message": "division by zero",
    }
    generated_fingerprint = fingerprint_error(raw_error)

    payload = {
        "fingerprint": generated_fingerprint,
        "error": raw_error,
    }

    req = EventIngestRequest.model_validate(payload)
    assert req.fingerprint == generated_fingerprint
    assert req.error.type == "ZeroDivisionError"
    assert req.error.message == "division by zero"
    assert req.error.traceback is None
    assert req.error.timestamp is None
    assert req.error.context is None


def test_02_missing_fingerprint():
    """Verify rejection when the top-level fingerprint is omitted."""
    raw_error = {"type": "ValueError", "message": "bad input"}
    payload = {"error": raw_error}

    with pytest.raises(ValidationError) as exc_info:
        EventIngestRequest.model_validate(payload)
    errors = exc_info.value.errors()
    assert any(err["loc"] == ("fingerprint",) and err["type"] == "missing" for err in errors)


def test_03_incorrect_fingerprint_length():
    """Verify rejection when fingerprint length is not exactly 64 characters."""
    raw_error = {"type": "KeyError", "message": "'token'"}
    # 63 characters
    with pytest.raises(ValidationError) as exc_short:
        EventIngestRequest.model_validate({
            "fingerprint": "a" * 63,
            "error": raw_error,
        })
    assert any("pattern" in err["type"] for err in exc_short.value.errors())

    # 65 characters
    with pytest.raises(ValidationError) as exc_long:
        EventIngestRequest.model_validate({
            "fingerprint": "a" * 65,
            "error": raw_error,
        })
    assert any("pattern" in err["type"] for err in exc_long.value.errors())


def test_04_non_hexadecimal_fingerprint():
    """Verify rejection when fingerprint contains non-hexadecimal characters or uppercase."""
    raw_error = {"type": "RuntimeError", "message": "unexpected state"}

    # Non-hex character 'g'
    with pytest.raises(ValidationError) as exc_non_hex:
        EventIngestRequest.model_validate({
            "fingerprint": "g" + "0" * 63,
            "error": raw_error,
        })
    assert any("pattern" in err["type"] for err in exc_non_hex.value.errors())

    # Uppercase hex characters (must be strictly lowercase)
    valid_fp = fingerprint_error(raw_error)
    with pytest.raises(ValidationError) as exc_upper:
        EventIngestRequest.model_validate({
            "fingerprint": valid_fp.upper(),
            "error": raw_error,
        })
    assert any("pattern" in err["type"] for err in exc_upper.value.errors())


def test_05_missing_error_object():
    """Verify rejection when error object is missing entirely or None."""
    fp = fingerprint_error({"type": "TestError", "message": "msg"})

    with pytest.raises(ValidationError) as exc_missing:
        EventIngestRequest.model_validate({"fingerprint": fp})
    assert any(err["loc"] == ("error",) for err in exc_missing.value.errors())

    with pytest.raises(ValidationError) as exc_none:
        EventIngestRequest.model_validate({"fingerprint": fp, "error": None})
    assert any(err["loc"] == ("error",) for err in exc_none.value.errors())


def test_06_missing_error_type():
    """Verify rejection when error type is missing or an empty string."""
    fp = fingerprint_error({"message": "missing type"})

    # Missing type key
    with pytest.raises(ValidationError) as exc_missing:
        EventIngestRequest.model_validate({
            "fingerprint": fp,
            "error": {"message": "something broke"},
        })
    assert any(err["loc"] == ("error", "type") for err in exc_missing.value.errors())

    # Empty string type
    with pytest.raises(ValidationError) as exc_empty:
        EventIngestRequest.model_validate({
            "fingerprint": fp,
            "error": {"type": "", "message": "something broke"},
        })
    assert any(err["loc"] == ("error", "type") for err in exc_empty.value.errors())


def test_07_missing_error_message():
    """Verify rejection when error message is missing."""
    fp = fingerprint_error({"type": "IndexError"})

    with pytest.raises(ValidationError) as exc_missing:
        EventIngestRequest.model_validate({
            "fingerprint": fp,
            "error": {"type": "IndexError"},
        })
    assert any(err["loc"] == ("error", "message") for err in exc_missing.value.errors())


def test_08_invalid_field_types():
    """Verify rejection when field types are invalid and not silently coerced."""
    raw_error = {"type": "TypeError", "message": "wrong type"}
    fp = fingerprint_error(raw_error)

    # Integer fingerprint instead of string
    with pytest.raises(ValidationError):
        EventIngestRequest.model_validate({"fingerprint": 123456789, "error": raw_error})

    # Integer error type instead of string
    with pytest.raises(ValidationError):
        EventIngestRequest.model_validate({
            "fingerprint": fp,
            "error": {"type": 500, "message": "error msg"},
        })

    # Boolean message instead of string
    with pytest.raises(ValidationError):
        EventIngestRequest.model_validate({
            "fingerprint": fp,
            "error": {"type": "ValueError", "message": True},
        })

    # Non-dictionary error object (e.g., string)
    with pytest.raises(ValidationError):
        EventIngestRequest.model_validate({
            "fingerprint": fp,
            "error": "not a dictionary",
        })

    # Non-dictionary context
    with pytest.raises(ValidationError):
        EventIngestRequest.model_validate({
            "fingerprint": fp,
            "error": {"type": "ValueError", "message": "msg", "context": "not-a-dict"},
        })


def test_09_unexpected_top_level_fields():
    """Verify strict rejection of unexpected top-level fields (extra='forbid')."""
    raw_error = {"type": "TypeError", "message": "forbidden extra"}
    fp = fingerprint_error(raw_error)

    with pytest.raises(ValidationError) as exc_info:
        EventIngestRequest.model_validate({
            "fingerprint": fp,
            "error": raw_error,
            "extra_field": "not_allowed",
            "injection_attempt": True,
        })
    errors = exc_info.value.errors()
    assert any(err["loc"] == ("extra_field",) and err["type"] == "extra_forbidden" for err in errors)


def test_10_valid_optional_error_metadata():
    """Verify that optional diagnostics and extra SDK metadata are accepted."""
    now = datetime.now(timezone.utc)
    raw_error = {
        "type": "CustomAppError",
        "message": "service timed out",
        "traceback": "Traceback (most recent call last):\n  File 'app.py', line 12",
        "timestamp": now.isoformat(),
        "context": {"user_id": 42, "env": "staging"},
        # Additional metadata for future SDK extensions:
        "sdk_version": "1.2.0",
        "host_architecture": "x86_64",
    }
    fp = fingerprint_error(raw_error)

    req = EventIngestRequest.model_validate({"fingerprint": fp, "error": raw_error})
    assert req.error.type == "CustomAppError"
    assert req.error.message == "service timed out"
    assert "File 'app.py'" in req.error.traceback
    assert req.error.timestamp == now
    assert req.error.context == {"user_id": 42, "env": "staging"}
    # Verify extra fields are preserved on ErrorDetail
    assert req.error.sdk_version == "1.2.0"
    assert req.error.host_architecture == "x86_64"


def test_11_valid_response_serialization():
    """Verify EventIngestResponse serialization and strict field compliance."""
    event_id = uuid4()
    queued_at = datetime.now(timezone.utc)
    raw_error = {"type": "HandledError", "message": "handled"}
    fp = fingerprint_error(raw_error)

    response = EventIngestResponse(
        status="accepted",
        event_id=event_id,
        fingerprint=fp,
        queued_at=queued_at,
    )

    # Serialization test
    dumped_dict = response.model_dump()
    assert dumped_dict["status"] == "accepted"
    assert dumped_dict["event_id"] == event_id
    assert dumped_dict["fingerprint"] == fp
    assert dumped_dict["queued_at"] == queued_at

    dumped_json = response.model_dump_json()
    parsed_json = json.loads(dumped_json)
    assert parsed_json["status"] == "accepted"
    assert parsed_json["event_id"] == str(event_id)
    assert parsed_json["fingerprint"] == fp

    # Roundtrip validation
    restored = EventIngestResponse.model_validate_json(dumped_json)
    assert restored == response


def test_12_invalid_or_timezone_naive_timestamps():
    """Verify that timezone-naive or malformed timestamps are rejected."""
    raw_error = {"type": "TimestampError", "message": "bad tz"}
    fp = fingerprint_error(raw_error)

    # Timezone-naive datetime in ErrorDetail
    with pytest.raises(ValidationError) as exc_naive:
        EventIngestRequest.model_validate({
            "fingerprint": fp,
            "error": {
                "type": "TimestampError",
                "message": "bad tz",
                "timestamp": "2026-11-23T00:00:00",  # No timezone offset
            },
        })
    assert any("timezone-aware" in str(err) for err in exc_naive.value.errors())

    # Completely invalid timestamp string in ErrorDetail
    with pytest.raises(ValidationError):
        EventIngestRequest.model_validate({
            "fingerprint": fp,
            "error": {
                "type": "TimestampError",
                "message": "bad tz",
                "timestamp": "not-a-timestamp",
            },
        })

    # Timezone-naive datetime in EventIngestResponse
    with pytest.raises(ValidationError) as exc_resp_naive:
        EventIngestResponse(
            status="accepted",
            event_id=uuid4(),
            fingerprint=fp,
            queued_at=datetime(2026, 11, 23, 0, 0, 0),  # Naive
        )
    assert any("timezone-aware" in str(err) for err in exc_resp_naive.value.errors())



def test_13_whitespace_only_error_type():
    """Verify that whitespace-only error type strings are rejected."""
    raw_error = {"type": "   \t \n ", "message": "whitespace type"}
    fp = fingerprint_error(raw_error)

    with pytest.raises(ValidationError) as exc_info:
        EventIngestRequest.model_validate({"fingerprint": fp, "error": raw_error})
    assert any("whitespace-only" in str(err) for err in exc_info.value.errors())


def test_14_numeric_timestamps_rejected():
    """Verify that integer, float, and numeric string timestamps are rejected."""
    raw_error = {
        "type": "TypeError",
        "message": "numeric ts",
        "timestamp": 1700000000,
    }
    fp = fingerprint_error(raw_error)

    # Integer epoch
    with pytest.raises(ValidationError) as exc_int:
        EventIngestRequest.model_validate({"fingerprint": fp, "error": raw_error})
    assert any("Numeric timestamps are not supported" in str(err) for err in exc_int.value.errors())

    # Float epoch
    with pytest.raises(ValidationError):
        EventIngestRequest.model_validate({
            "fingerprint": fp,
            "error": {"type": "TypeError", "message": "msg", "timestamp": 1700000000.5},
        })

    # Numeric string epoch (e.g., "1700000000")
    with pytest.raises(ValidationError) as exc_str:
        EventIngestRequest.model_validate({
            "fingerprint": fp,
            "error": {"type": "TypeError", "message": "msg", "timestamp": "1700000000"},
        })
    assert any("Numeric timestamp strings are not supported" in str(err) for err in exc_str.value.errors())

    # Float numeric string epoch (e.g., "1700000000.5")
    with pytest.raises(ValidationError):
        EventIngestRequest.model_validate({
            "fingerprint": fp,
            "error": {"type": "TypeError", "message": "msg", "timestamp": "1700000000.5"},
        })


def test_15_non_finite_numbers_rejected():
    """Verify that NaN, Infinity, and -Infinity are rejected in context and metadata."""
    fp = fingerprint_error({"type": "MathError", "message": "overflow"})

    # NaN in context
    with pytest.raises(ValidationError) as exc_nan:
        EventIngestRequest.model_validate({
            "fingerprint": fp,
            "error": {
                "type": "MathError",
                "message": "overflow",
                "context": {"loss": float("nan")},
            },
        })
    assert any("Non-finite" in str(err) for err in exc_nan.value.errors())

    # Infinity in extra metadata
    with pytest.raises(ValidationError) as exc_inf:
        EventIngestRequest.model_validate({
            "fingerprint": fp,
            "error": {
                "type": "MathError",
                "message": "overflow",
                "grad_norm": float("inf"),
            },
        })
    assert any("Non-finite" in str(err) for err in exc_inf.value.errors())


def test_16_invalid_metadata_values_rejected():
    """Verify that non-JSON-compatible types (sets, functions) are rejected."""
    fp = fingerprint_error({"type": "ValueError", "message": "bad data"})

    with pytest.raises(ValidationError) as exc_set:
        EventIngestRequest.model_validate({
            "fingerprint": fp,
            "error": {
                "type": "ValueError",
                "message": "bad data",
                "context": {"bad_set": {1, 2, 3}},
            },
        })
    assert any("not JSON-compatible" in str(err) for err in exc_set.value.errors())
