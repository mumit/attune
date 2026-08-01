"""Tests for the hardened HTTP retry helper (build prompt 33's own finding:
"_fetch_with_retry is currently a bare 3-shot loop with no delay")."""

from __future__ import annotations

import pytest

from attune.retry import retry_call


class _RetryAfterError(Exception):
    def __init__(self, seconds):
        super().__init__("rate limited")
        self.resp = _Resp(seconds)


class _Resp:
    def __init__(self, retry_after):
        self._retry_after = retry_after

    def get(self, key, default=None):
        if key == "retry-after":
            return self._retry_after
        return default


def test_retry_call_returns_first_success_without_sleeping():
    sleeps = []
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return "ok"

    result = retry_call(fn, sleep=sleeps.append)

    assert result == "ok"
    assert calls["n"] == 1
    assert sleeps == []


def test_retry_call_retries_bounded_times_then_raises():
    sleeps = []
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise ValueError("boom")

    with pytest.raises(ValueError):
        retry_call(fn, retries=2, sleep=sleeps.append, rand=lambda: 0.0)

    # retries=2 -> 3 total attempts, 2 sleeps between them.
    assert calls["n"] == 3
    assert len(sleeps) == 2


def test_retry_call_succeeds_after_transient_failures():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("transient")
        return "recovered"

    result = retry_call(fn, retries=5, sleep=lambda _: None, rand=lambda: 0.0)

    assert result == "recovered"
    assert calls["n"] == 3


def test_retry_call_honours_retry_after_over_backoff():
    sleeps = []

    def fn():
        raise _RetryAfterError(7.5)

    with pytest.raises(_RetryAfterError):
        retry_call(fn, retries=1, sleep=sleeps.append, rand=lambda: 0.0)

    assert sleeps == [7.5]


def test_retry_call_falls_back_to_jittered_backoff_without_retry_after():
    sleeps = []

    def fn():
        raise ValueError("no retry-after here")

    with pytest.raises(ValueError):
        retry_call(
            fn, retries=2, base_delay=0.1, sleep=sleeps.append, rand=lambda: 0.5,
        )

    # attempt 0: 0.1 * 2**0 + 0.5*0.1 = 0.15; attempt 1: 0.1*2**1 + 0.5*0.1 = 0.25
    assert sleeps == pytest.approx([0.15, 0.25])
