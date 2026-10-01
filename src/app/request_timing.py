"""Per-request accounting of time spent in provider API calls.

The performance middleware starts a tally for each web request; the provider
request helpers add to it. Outside a web request (Celery, management commands)
no tally exists and recording does nothing.
"""

import contextvars
import functools
import time

_tally = contextvars.ContextVar("request_provider_tally", default=None)
# api_request retries by calling itself and calls resilient_request; only the
# outermost call is timed so the same wait is never counted twice.
_inside_call = contextvars.ContextVar("request_provider_inside_call", default=False)


def begin():
    """Start a tally for the current request and return it with its reset token."""
    tally = {"calls": 0, "seconds": 0.0}
    return tally, _tally.set(tally)


def end(token):
    """Stop the tally started by begin()."""
    _tally.reset(token)


def timed_provider_call(func):
    """Add the wrapped call's wall time (waits and retries included) to the tally."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        tally = _tally.get()
        if tally is None or _inside_call.get():
            return func(*args, **kwargs)
        marker = _inside_call.set(True)
        started = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            _inside_call.reset(marker)
            tally["calls"] += 1
            tally["seconds"] += time.perf_counter() - started

    return wrapper
