"""Non-blocking HTTP error reporting."""

from __future__ import annotations

import json
import queue
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from .cache import DuplicateErrorCache
from .fingerprint import fingerprint_error

ErrorPayload = Mapping[str, Any]
ErrorHandler = Callable[[Exception], None]


class ErrorReporter:
    """Queue unique errors for delivery from a daemon thread."""

    def __init__(
        self,
        endpoint_url: str,
        *,
        cache: DuplicateErrorCache | None = None,
        timeout: float = 5.0,
        queue_size: int = 1_000,
        error_handler: ErrorHandler | None = None,
    ) -> None:
        if not endpoint_url:
            raise ValueError("endpoint_url must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be greater than 0")
        if queue_size < 1:
            raise ValueError("queue_size must be at least 1")

        self._endpoint_url = endpoint_url
        self._cache = cache or DuplicateErrorCache()
        self._timeout = timeout
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(
            maxsize=queue_size
        )
        self._error_handler = error_handler
        self._state_lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name="shadowheal-error-reporter",
            daemon=True,
        )
        self._thread.start()

    def report(self, error: ErrorPayload) -> bool:
        """Enqueue a unique error without waiting for the network."""
        fingerprint = fingerprint_error(error)
        payload = {"fingerprint": fingerprint, "error": dict(error)}
        with self._state_lock:
            if self._closed or not self._cache.add_if_new(fingerprint):
                return False
            try:
                self._queue.put_nowait(payload)
            except queue.Full as exc:
                self._cache.discard(fingerprint)
                self._notify_error(exc)
                return False
            return True

    def close(self, timeout: float | None = None) -> None:
        """Stop the worker after queued errors have been processed."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._queue.join()
        self._queue.put(None)
        self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                self._send(item)
            finally:
                self._queue.task_done()

    def _send(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._endpoint_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout):
                pass
        except (OSError, urllib.error.URLError) as exc:
            self._notify_error(exc)

    def _notify_error(self, error: Exception) -> None:
        if self._error_handler is not None:
            self._error_handler(error)
