"""Thread-safe in-memory duplicate suppression."""

from __future__ import annotations

from collections import OrderedDict
from threading import Lock


class DuplicateErrorCache:
    """Track fingerprints and accept each fingerprint once."""

    def __init__(self, max_entries: int = 10_000) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._fingerprints: OrderedDict[str, None] = OrderedDict()
        self._lock = Lock()
        self._max_entries = max_entries

    def add_if_new(self, fingerprint: str) -> bool:
        """Store a fingerprint and return whether it was not already present."""
        with self._lock:
            if fingerprint in self._fingerprints:
                return False
            self._fingerprints[fingerprint] = None
            if len(self._fingerprints) > self._max_entries:
                self._fingerprints.popitem(last=False)
            return True

    def discard(self, fingerprint: str) -> None:
        """Remove a fingerprint when its queued item could not be accepted."""
        with self._lock:
            self._fingerprints.pop(fingerprint, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._fingerprints)
