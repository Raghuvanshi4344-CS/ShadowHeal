import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import threading
import time
import unittest
from unittest.mock import patch
import sys
from urllib.request import Request
from urllib.error import HTTPError

from shadowheal_sdk import (
    DuplicateErrorCache,
    ErrorReporter,
    capture_exception,
    fingerprint_error,
    install_excepthook,
)


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_is_stable_for_mapping_order(self):
        first = {"message": "failed", "code": 500}
        second = {"code": 500, "message": "failed"}

        self.assertEqual(fingerprint_error(first), fingerprint_error(second))

    def test_fingerprint_is_sha256_of_canonical_json(self):
        error = {"message": "failed", "code": 500}
        canonical = json.dumps(
            error, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

        self.assertEqual(
            fingerprint_error(error),
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )

    def test_fingerprint_is_64_character_hex_digest(self):
        digest = fingerprint_error({"message": "failed"})
        self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_different_normal_signatures_have_different_fingerprints(self):
        first = fingerprint_error({"type": "ValueError", "message": "bad"})
        second = fingerprint_error({"type": "ValueError", "message": "worse"})
        self.assertNotEqual(first, second)

    def test_type_and_stack_are_part_of_the_signature(self):
        first = {
            "type": "ValueError",
            "message": "bad",
            "stack": [{"filename": "a.py", "line": 10, "function": "run"}],
        }
        changed_type = dict(first, type="RuntimeError")
        changed_stack = dict(first, stack=[{"filename": "b.py", "line": 20, "function": "run"}])

        self.assertNotEqual(fingerprint_error(first), fingerprint_error(changed_type))
        self.assertNotEqual(fingerprint_error(first), fingerprint_error(changed_stack))

    def test_empty_and_unusual_messages_are_supported(self):
        values = [
            {"type": "ValueError", "message": ""},
            {"type": "UnicodeError", "message": "snowman: \u2603"},
            {"type": "ValueError", "message": None},
            {"type": "CustomError", "message": object()},
        ]
        digests = [fingerprint_error(value) for value in values]
        self.assertTrue(all(len(digest) == 64 for digest in digests))
        self.assertEqual(len(set(digests)), len(digests))

    def test_repeated_execution_is_deterministic(self):
        error = {"type": "KeyError", "message": "missing", "stack": ["line 1", "line 2"]}
        first = fingerprint_error(error)
        for _ in range(100):
            self.assertEqual(first, fingerprint_error(error))


class CacheTests(unittest.TestCase):
    def test_repeated_error_is_accepted_once(self):
        cache = DuplicateErrorCache()

        accepted = [cache.add_if_new("same-error") for _ in range(500)]

        self.assertEqual(sum(accepted), 1)

    def test_cache_is_thread_safe(self):
        cache = DuplicateErrorCache()
        results = []

        def add_error():
            results.append(cache.add_if_new("same-error"))

        threads = [threading.Thread(target=add_error) for _ in range(100)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(results), 1)

    def test_500_identical_errors_are_accepted_once(self):
        cache = DuplicateErrorCache()
        accepted = [cache.add_if_new("identical") for _ in range(500)]
        self.assertEqual(sum(accepted), 1)
        self.assertEqual(len(cache), 1)

    def test_different_errors_are_not_suppressed(self):
        cache = DuplicateErrorCache()
        self.assertTrue(cache.add_if_new("error-a"))
        self.assertTrue(cache.add_if_new("error-b"))
        self.assertTrue(cache.add_if_new("error-c"))
        self.assertEqual(len(cache), 3)

    def test_cache_evicts_oldest_entry_at_max_size(self):
        cache = DuplicateErrorCache(max_entries=2)
        self.assertTrue(cache.add_if_new("a"))
        self.assertTrue(cache.add_if_new("b"))
        self.assertTrue(cache.add_if_new("c"))
        self.assertFalse(cache.add_if_new("b"))
        self.assertTrue(cache.add_if_new("a"))
        self.assertEqual(len(cache), 2)


class ReporterTests(unittest.TestCase):
    @patch("shadowheal_sdk.reporter.urllib.request.urlopen")
    def test_report_sends_unique_error_in_background(self, urlopen):
        sent = threading.Event()

        def capture(request: Request, timeout: float):
            self.assertEqual(timeout, 2.0)
            self.assertEqual(request.method, "POST")
            sent.set()

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            return Response()

        urlopen.side_effect = capture
        reporter = ErrorReporter("https://example.test/errors", timeout=2.0)
        try:
            self.assertTrue(reporter.report({"message": "failed"}))
            self.assertFalse(reporter.report({"message": "failed"}))
            self.assertTrue(sent.wait(1.0))
        finally:
            reporter.close(timeout=1.0)

        self.assertEqual(urlopen.call_count, 1)

    @patch("shadowheal_sdk.reporter.urllib.request.urlopen")
    def test_http_failure_is_reported_without_reaching_caller(self, urlopen):
        reported = []
        urlopen.side_effect = OSError("network unavailable")
        reporter = ErrorReporter(
            "https://example.test/errors", error_handler=reported.append
        )
        try:
            self.assertTrue(reporter.report({"message": "failed"}))
            deadline = time.time() + 1.0
            while not reported and time.time() < deadline:
                time.sleep(0.01)
        finally:
            reporter.close(timeout=1.0)

        self.assertEqual(len(reported), 1)
        self.assertIsInstance(reported[0], OSError)

    def test_report_after_close_is_rejected(self):
        reporter = ErrorReporter("https://example.test/errors")
        reporter.close(timeout=1.0)

        self.assertFalse(reporter.report({"message": "late"}))

    @patch("shadowheal_sdk.reporter.urllib.request.urlopen")
    def test_payload_and_configured_endpoint_are_used(self, urlopen):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        urlopen.return_value = Response()
        reporter = ErrorReporter("http://127.0.0.1:8123/api/errors", timeout=1.25)
        try:
            self.assertTrue(reporter.report({
                "type": "ValueError",
                "message": "bad",
                "stack": [{"filename": "app.py", "line": 7, "function": "run"}],
            }))
            time.sleep(0.05)
        finally:
            reporter.close(timeout=1.0)

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8123/api/errors")
        self.assertEqual(request.method, "POST")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["error"]["type"], "ValueError")
        self.assertEqual(len(payload["fingerprint"]), 64)

    @patch("shadowheal_sdk.reporter.urllib.request.urlopen")
    def test_concurrent_duplicate_reports_dispatch_once(self, urlopen):
        sent = threading.Event()

        def capture(request: Request, timeout: float):
            sent.set()

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            return Response()

        urlopen.side_effect = capture
        reporter = ErrorReporter("https://example.test/errors")
        results = []

        def report():
            results.append(reporter.report({"type": "ValueError", "message": "bad"}))

        threads = [threading.Thread(target=report) for _ in range(100)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        try:
            self.assertEqual(sum(results), 1)
            self.assertTrue(sent.wait(1.0))
        finally:
            reporter.close(timeout=1.0)
        self.assertEqual(urlopen.call_count, 1)

    @patch("shadowheal_sdk.reporter.urllib.request.urlopen")
    def test_http_error_response_is_handled_without_crash(self, urlopen):
        reported = []
        urlopen.side_effect = HTTPError(
            "http://example.test/errors",
            503,
            "unavailable",
            {},
            None,
        )
        reporter = ErrorReporter(
            "http://example.test/errors",
            error_handler=reported.append,
        )
        try:
            self.assertTrue(reporter.report({"message": "failed"}))
            deadline = time.time() + 1.0
            while not reported and time.time() < deadline:
                time.sleep(0.01)
        finally:
            reporter.close(timeout=1.0)
        self.assertEqual(len(reported), 1)
        self.assertIsInstance(reported[0], HTTPError)

    @patch("shadowheal_sdk.reporter.urllib.request.urlopen")
    def test_handler_exception_does_not_kill_daemon_thread(self, urlopen):
        def raise_handler_error(error):
            raise RuntimeError("handler failed")

        urlopen.side_effect = OSError("network unavailable")
        reporter = ErrorReporter(
            "https://example.test/errors",
            error_handler=raise_handler_error,
        )
        try:
            self.assertTrue(reporter.report({"message": "first"}))
            time.sleep(0.05)
            self.assertTrue(reporter.report({"message": "second"}))
        finally:
            reporter.close(timeout=1.0)
        self.assertEqual(urlopen.call_count, 2)

    @patch("shadowheal_sdk.reporter.urllib.request.urlopen")
    def test_unusual_payload_is_json_serializable_at_send_time(self, urlopen):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        urlopen.return_value = Response()
        reporter = ErrorReporter("https://example.test/errors")
        try:
            self.assertTrue(reporter.report({"message": object()}))
            time.sleep(0.05)
        finally:
            reporter.close(timeout=1.0)
        self.assertEqual(urlopen.call_count, 1)


class ExceptionHookTests(unittest.TestCase):
    def setUp(self):
        self.original_hook = sys.excepthook

    def tearDown(self):
        sys.excepthook = self.original_hook

    def test_unhandled_exception_is_intercepted_and_original_hook_is_called(self):
        with patch("shadowheal_sdk.reporter.urllib.request.urlopen"):
            reporter = ErrorReporter("https://example.test/errors")
            calls = []

            def previous(exc_type, exc, tb):
                calls.append((exc_type, exc, tb))

            sys.excepthook = previous
            hook = install_excepthook(reporter)
            try:
                try:
                    raise ValueError("bad")
                except ValueError as exc:
                    exc_type = type(exc)
                    tb = exc.__traceback__
                    hook(exc_type, exc, tb)
            finally:
                reporter.close(timeout=1.0)

            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], ValueError)

    def test_repeated_install_does_not_replace_hook(self):
        reporter = ErrorReporter("https://example.test/errors")
        try:
            first = install_excepthook(reporter)
            second = install_excepthook(reporter)
            self.assertIs(first, second)
        finally:
            reporter.close(timeout=1.0)

    def test_capture_exception_extracts_type_message_and_stack(self):
        with patch("shadowheal_sdk.reporter.urllib.request.urlopen"):
            reporter = ErrorReporter("https://example.test/errors")
            try:
                try:
                    raise KeyError("missing")
                except KeyError as exc:
                    accepted = capture_exception(
                        reporter,
                        type(exc),
                        exc,
                        exc.__traceback__,
                    )
                    fingerprint = reporter._cache
            finally:
                reporter.close(timeout=1.0)

        self.assertTrue(accepted)
        self.assertEqual(len(fingerprint), 1)


class LocalServerE2ETests(unittest.TestCase):
    def test_500_identical_and_one_different_error(self):
        requests_received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                requests_received.append((self.path, body))
                self.send_response(204)
                self.end_headers()

            def log_message(self, format, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        reporter = ErrorReporter(
            f"http://127.0.0.1:{server.server_port}/api/errors",
            timeout=2.0,
        )
        try:
            dispatches = [
                reporter.report({"type": "ValueError", "message": "bad"})
                for _ in range(500)
            ]
            self.assertTrue(reporter.report({"type": "RuntimeError", "message": "different"}))
            reporter.close(timeout=3.0)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=1.0)

        dispatched = sum(dispatches)
        suppressed = 500 - dispatched
        print(
            f"500-error duplicate test: dispatched={dispatched}, "
            f"suppressed={suppressed}, outbound_requests={len(requests_received)}"
        )

        self.assertEqual(dispatched, 1)
        self.assertEqual(suppressed, 499)
        self.assertEqual(len(requests_received), 2)
