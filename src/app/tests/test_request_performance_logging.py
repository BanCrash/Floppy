"""Tests for RequestPerformanceLoggingMiddleware."""

import re
import time
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.db import connection
from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from django.test.utils import override_settings

from app import request_timing
from app.middleware import RequestPerformanceLoggingMiddleware
from app.models import Item
from app.providers import pocketcasts
from users.models import User


class RequestPerformanceLoggingMiddlewareTests(TestCase):
    """Verify slow/query-heavy requests are logged and fast ones are not."""

    def setUp(self):
        """Create a request factory."""
        self.factory = RequestFactory()

    def _run(self, view):
        middleware = RequestPerformanceLoggingMiddleware(view)
        return middleware(self.factory.get("/test-path"))

    @override_settings(
        PERF_LOG_ENABLED=True,
        PERF_LOG_SLOW_REQUEST_MS=0,
        PERF_LOG_QUERY_COUNT_THRESHOLD=10_000,
    )
    def test_logs_slow_request(self):
        """A request over the duration threshold is logged."""
        with self.assertLogs("app.middleware", level="INFO") as logs:
            response = self._run(lambda _request: HttpResponse("ok"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("slow_request", logs.output[0])
        self.assertIn("path=/test-path", logs.output[0])

    @override_settings(
        PERF_LOG_ENABLED=True,
        PERF_LOG_SLOW_REQUEST_MS=10_000,
        PERF_LOG_QUERY_COUNT_THRESHOLD=1,
    )
    def test_logs_query_heavy_request(self):
        """A request over the query-count threshold is logged."""

        def view(_request):
            list(Item.objects.all())
            return HttpResponse("ok")

        with self.assertLogs("app.middleware", level="INFO") as logs:
            self._run(view)
        self.assertIn("queries=1", logs.output[0])

    @override_settings(
        PERF_LOG_ENABLED=True,
        PERF_LOG_SLOW_REQUEST_MS=10_000,
        PERF_LOG_QUERY_COUNT_THRESHOLD=10_000,
    )
    def test_fast_request_not_logged(self):
        """A fast, light request is not logged."""
        with self.assertNoLogs("app.middleware", level="INFO"):
            self._run(lambda _request: HttpResponse("ok"))

    @override_settings(PERF_LOG_ENABLED=False, PERF_LOG_SLOW_REQUEST_MS=0)
    def test_disabled_via_setting(self):
        """The middleware is a no-op when disabled."""
        with self.assertNoLogs("app.middleware", level="INFO"):
            response = self._run(lambda _request: HttpResponse("ok"))
        self.assertEqual(response.status_code, 200)


@override_settings(
    PERF_LOG_ENABLED=True,
    PERF_LOG_SLOW_REQUEST_MS=0,
    PERF_LOG_QUERY_COUNT_THRESHOLD=10_000,
)
class RequestTimingBreakdownTests(TestCase):
    """The log line and Server-Timing header say where a request's time went."""

    def setUp(self):
        """Create a request factory and a signed-in user."""
        self.factory = RequestFactory()
        self.user = User.objects.create_user(username="timer", password="pw")

    def _run(self, view, user=None):
        request = self.factory.get("/test-path")
        request.user = user or AnonymousUser()
        with self.assertLogs("app.middleware", level="INFO") as logs:
            response = RequestPerformanceLoggingMiddleware(view)(request)
        return response, logs.output[0]

    @staticmethod
    def _field(line, name):
        return float(re.search(rf"{name}=([0-9.]+)", line).group(1))

    def test_log_line_carries_the_breakdown_fields(self):
        _response, line = self._run(lambda _request: HttpResponse("ok"))

        for name in ("cpu_ms", "db_ms", "provider_ms", "provider_calls", "inflight"):
            self.assertIn(f"{name}=", line)
        self.assertEqual(self._field(line, "inflight"), 1)

    def test_provider_time_is_counted_once_for_nested_calls(self):
        @request_timing.timed_provider_call
        def inner():
            time.sleep(0.03)

        @request_timing.timed_provider_call
        def outer():
            inner()
            inner()

        def view(_request):
            outer()
            return HttpResponse("ok")

        _response, line = self._run(view)

        self.assertEqual(self._field(line, "provider_calls"), 1)
        self.assertGreaterEqual(self._field(line, "provider_ms"), 55)
        self.assertLess(self._field(line, "provider_ms"), 200)

    def test_waiting_is_not_reported_as_cpu(self):
        def view(_request):
            time.sleep(0.05)
            return HttpResponse("ok")

        _response, line = self._run(view)

        self.assertGreaterEqual(self._field(line, "duration_ms"), 50)
        self.assertLess(self._field(line, "cpu_ms"), 40)

    def test_database_time_is_counted(self):
        def slow_query(execute, sql, params, many, context):
            # A query on an empty table finishes in well under the 0.1 ms the
            # log line rounds to, which made this test report db_ms=0.0 on a
            # fast runner. Make the query take measurable time.
            time.sleep(0.02)
            return execute(sql, params, many, context)

        def view(_request):
            with connection.execute_wrapper(slow_query):
                list(Item.objects.all())
            return HttpResponse("ok")

        _response, line = self._run(view)

        self.assertEqual(self._field(line, "queries"), 1)
        self.assertGreaterEqual(self._field(line, "db_ms"), 15)

    def test_server_timing_header_for_signed_in_users_only(self):
        response, _line = self._run(lambda _request: HttpResponse("ok"), self.user)
        self.assertRegex(
            response["Server-Timing"],
            r"^total;dur=\d+, cpu;dur=[\d.]+, db;dur=[\d.]+, provider;dur=[\d.]+$",
        )

        anonymous_response, _line = self._run(lambda _request: HttpResponse("ok"))
        self.assertNotIn("Server-Timing", anonymous_response)

    def test_providers_that_call_requests_directly_are_counted(self):
        def slow_get(*_args, **_kwargs):
            time.sleep(0.03)
            response = mock.Mock()
            response.json.return_value = {"results": []}
            return response

        def view(_request):
            pocketcasts.search("news", 1)
            return HttpResponse("ok")

        with mock.patch("app.providers.pocketcasts.requests.get", slow_get):
            _response, line = self._run(view)

        self.assertEqual(self._field(line, "provider_calls"), 1)
        self.assertGreaterEqual(self._field(line, "provider_ms"), 25)

    def test_recording_outside_a_request_does_nothing(self):
        @request_timing.timed_provider_call
        def call():
            return "result"

        self.assertEqual(call(), "result")
