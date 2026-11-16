import hashlib
import json
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request

from shadowheal_sdk import DuplicateErrorCache, ErrorReporter, fingerprint_error


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
