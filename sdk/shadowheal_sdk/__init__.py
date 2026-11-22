"""Asynchronous error reporting for ShadowHeal."""

from __future__ import annotations

import sys
from types import TracebackType
from typing import Callable

from .cache import DuplicateErrorCache
from .fingerprint import fingerprint_error
from .reporter import ErrorReporter

__all__ = [
    "DuplicateErrorCache",
    "ErrorReporter",
    "capture_exception",
    "fingerprint_error",
    "install_excepthook",
]


def capture_exception(
    reporter: ErrorReporter,
    exc_type: type[BaseException],
    exc: BaseException,
    tb: TracebackType | None,
) -> bool:
    """Extract exception details and submit them through a reporter."""
    stack = []
    current = tb
    while current is not None:
        frame = current.tb_frame
        stack.append(
            {
                "filename": frame.f_code.co_filename,
                "line": current.tb_lineno,
                "function": frame.f_code.co_name,
            }
        )
        current = current.tb_next

    return reporter.report(
        {
            "type": exc_type.__name__,
            "message": str(exc),
            "stack": stack,
        }
    )


def install_excepthook(
    reporter: ErrorReporter,
) -> Callable[[type[BaseException], BaseException, TracebackType | None], None]:
    """Install a non-recursive global exception hook for this reporter."""
    marker = getattr(sys.excepthook, "_shadowheal_reporter", None)
    if marker is reporter:
        return sys.excepthook

    previous = sys.excepthook

    def shadowheal_excepthook(
        exc_type: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
    ) -> None:
        try:
            capture_exception(reporter, exc_type, exc, tb)
        except Exception:
            pass

        try:
            previous(exc_type, exc, tb)
        except Exception:
            pass

    setattr(shadowheal_excepthook, "_shadowheal_reporter", reporter)
    sys.excepthook = shadowheal_excepthook
    return shadowheal_excepthook
