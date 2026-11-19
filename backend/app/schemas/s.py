"""Pydantic schemas for ShadowHeal ingestion API payload validation."""

from datetime import datetime
import math
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    field_validator,
    model_validator,
)

FINGERPRINT_PATTERN = r"^[0-9a-f]{64}$"


def _validate_json_finite(val: Any) -> None:
    """Recursively verify that metadata values are strictly JSON-compliant and finite.

    Error messages are kept generic to avoid reflecting user-controlled keys or values.
    """
    if val is None or isinstance(val, (str, bool, int)):
        return
    if isinstance(val, float):
        if not math.isfinite(val):
            raise ValueError("Non-finite numeric values (NaN/Infinity) are not permitted in metadata")
        return
    if isinstance(val, (list, tuple)):
        for item in val:
            _validate_json_finite(item)
        return
    if isinstance(val, dict):
        for k, v in val.items():
            if not isinstance(k, str):
                raise ValueError("All metadata dictionary keys must be strings")
            _validate_json_finite(v)
        return
    raise ValueError(f"Metadata values of type '{type(val).__name__}' are not JSON-compatible")


class ErrorDetail(BaseModel):
    """Detailed error and exception context sent by the SDK.

    Accepts core required error attributes and optional diagnostic metadata.
    Permits extra JSON-compatible metadata to remain forward-compatible with
    SDK evolutions. Rejects whitespace-only types, numeric timestamps, and non-finite numbers.
    """

    model_config = ConfigDict(extra="allow")

    type: StrictStr = Field(
        ...,
        min_length=1,
        description="Non-empty error or exception type name (e.g., ValueError).",
    )
    message: StrictStr = Field(
        ...,
        description="Error or exception message description.",
    )
    traceback: StrictStr | None = Field(
        default=None,
        description="Optional formatted stack trace string.",
    )
    timestamp: datetime | None = Field(
        default=None,
        description="Optional timezone-aware timestamp when the failure occurred.",
    )
    context: dict[str, Any] | None = Field(
        default=None,
        description="Optional execution context (local variables, runtime metrics, etc.).",
    )

    @field_validator("type")
    @classmethod
    def validate_type_non_whitespace(cls, value: str) -> str:
        """Reject empty or whitespace-only error type strings."""
        if not value.strip():
            raise ValueError("type cannot be empty or whitespace-only")
        return value

    @field_validator("timestamp", mode="before")
    @classmethod
    def validate_timestamp_format(cls, value: Any) -> Any:
        """Reject numeric timestamps (integers, floats, booleans, numeric strings); require ISO 8601 strings."""
        if isinstance(value, (int, float, bool)):
            raise ValueError("Numeric timestamps are not supported; use ISO 8601 timezone-aware strings.")
        if isinstance(value, str):
            try:
                float(value)
            except ValueError:
                pass
            else:
                raise ValueError("Numeric timestamp strings are not supported; use ISO 8601 timezone-aware strings.")
        return value

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp_tz(cls, value: datetime | None) -> datetime | None:
        """Ensure provided timestamp contains valid timezone information."""
        if value is not None and (value.tzinfo is None or value.tzinfo.utcoffset(value) is None):
            raise ValueError("timestamp must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_metadata_json_compatibility(self) -> "ErrorDetail":
        """Verify context and extra metadata contain only JSON-compliant, finite values."""
        if self.context is not None:
            _validate_json_finite(self.context)
        extra_fields = self.__pydantic_extra__ or {}
        for v in extra_fields.values():
            _validate_json_finite(v)
        return self


class EventIngestRequest(BaseModel):
    """Payload contract for incoming error events sent to POST /v1/events/ingest.

    Strictly enforces required top-level attributes and rejects unexpected fields.
    """

    model_config = ConfigDict(extra="forbid")

    fingerprint: StrictStr = Field(
        ...,
        pattern=FINGERPRINT_PATTERN,
        description="Deterministic 64-character lowercase hexadecimal SHA-256 hash.",
    )
    error: ErrorDetail = Field(
        ...,
        description="Validated ErrorDetail object.",
    )


class EventIngestResponse(BaseModel):
    """Response contract returned upon successful event enqueueing (HTTP 202 Accepted)."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"] = Field(
        default="accepted",
        description="Acknowledgement status confirming queue acceptance.",
    )
    event_id: UUID = Field(
        ...,
        description="Unique UUID assigned to the queued event.",
    )
    fingerprint: StrictStr = Field(
        ...,
        pattern=FINGERPRINT_PATTERN,
        description="Valid 64-character hexadecimal SHA-256 error fingerprint.",
    )
    queued_at: datetime = Field(
        ...,
        description="Timezone-aware timestamp recording when the event was queued.",
    )

    @field_validator("queued_at")
    @classmethod
    def validate_queued_at_tz(cls, value: datetime) -> datetime:
        """Ensure queued_at contains timezone information."""
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("queued_at must be timezone-aware")
        return value
