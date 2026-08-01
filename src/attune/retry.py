"""A hardened retry helper for outbound HTTP calls (Phase P7, build prompt
33 — the "Constraints" section's own finding: "`_fetch_with_retry` is
currently a bare 3-shot loop with no delay").

Distinct from ``llm.call_with_retry``: that helper is for model calls, gated
behind ``ModelCapabilities.max_retries`` (off by default, since a model
gateway call is metered and opt-in retry avoids silently multiplying spend).
Connector-layer HTTP calls (Google API reads, and ``dispatcher.py``'s
per-item Gmail/Calendar fetches) have no equivalent capability gate and, pre-
existing, always retried unconditionally (the bare loop this replaces) — so
:func:`retry_call` keeps retrying ON by default, just with backoff, jitter,
and ``Retry-After`` awareness added rather than an immediate bare re-call.
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, TypeVar

T = TypeVar("T")

# Matches dispatcher.py's pre-existing FETCH_RETRIES default (2 extra
# attempts, 3 total) -- this helper changes the delay between attempts, not
# the attempt count, unless a caller opts into something different.
DEFAULT_RETRIES = 2
DEFAULT_BASE_DELAY = 0.2
DEFAULT_MAX_DELAY = 5.0


def _retry_after_seconds(exc: Exception) -> "float | None":
    """Extract a ``Retry-After`` delay (seconds) from a Google
    ``HttpError``-shaped exception, if present. Defensive and import-free:
    works whether or not ``google-api-python-client`` is installed, and
    never raises -- a malformed/missing header must never break a retry."""
    resp = getattr(exc, "resp", None)
    if resp is None:
        return None
    raw: Any = None
    getter = getattr(resp, "get", None)
    if callable(getter):
        try:
            raw = getter("retry-after")
        except Exception:  # noqa: BLE001 - defensive extraction only
            raw = None
    if raw is None:
        headers = getattr(resp, "headers", None)
        if headers is not None:
            try:
                raw = headers.get("retry-after") or headers.get("Retry-After")
            except Exception:  # noqa: BLE001
                raw = None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def retry_call(
    fn: Callable[[], T],
    *,
    retries: int = DEFAULT_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
) -> T:
    """Call ``fn``, retrying on any exception up to ``retries`` additional
    times (``retries + 1`` total attempts, matching the bare loop this
    replaces). Between attempts: honour a ``Retry-After`` value when the
    exception exposes one (Google's ``HttpError``), else jittered
    exponential backoff capped at ``max_delay``. Raises the last exception
    once attempts are exhausted.

    ``sleep``/``rand`` are injectable so tests can assert bounded attempt
    counts without real wall-clock delay -- mirrors ``llm.call_with_retry``'s
    own injection posture.
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - retry on any transient failure
            last_exc = exc
            if attempt >= retries:
                break
            retry_after = _retry_after_seconds(exc)
            delay = (
                retry_after
                if retry_after is not None
                else min(base_delay * (2**attempt), max_delay) + rand() * base_delay
            )
            sleep(delay)
    assert last_exc is not None  # loop always returns or sets last_exc first
    raise last_exc
