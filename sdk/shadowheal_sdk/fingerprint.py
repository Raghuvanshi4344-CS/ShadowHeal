"""Stable error fingerprint generation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def fingerprint_error(error: Mapping[str, Any]) -> str:
    """Return a deterministic SHA-256 fingerprint for an error payload."""
    canonical_error = json.dumps(
        error,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical_error.encode("utf-8")).hexdigest()
