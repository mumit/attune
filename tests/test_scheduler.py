"""Tests for scheduler.py — deterministic under an injected clock, no threads
(the run_loop shell is pragma: no cover; everything it calls is tested here).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from attune.scheduler import Job, Scheduler, daily_at, every

UTC = timezone.utc


# ---------------------------------------------------------------------------
# next-run math
# ---------------------------------------------------------------------------


def test_daily_at_same_day_when_time_still_ahead():
    now = datetime(2026, 7, 10, 5, 0, tzinfo=UTC)
    nxt = daily_at("07:30", "UTC")(now)
    assert nxt == datetime(2026, 7, 10, 7, 30, tzinfo=UTC)


def test_daily_at_rolls_to_tomorrow_when_passed():
    now = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    nxt = daily_at("07:30", "UTC")(now)
    assert nxt == datetime(2026, 7, 11, 7, 30, tzinfo=UTC)


def test_daily_at_respects_timezone():
    # 07:30 in Vancouver (PDT, UTC-7 in July) = 14:30 UTC. At 14:00 UTC it's
    # still ahead today; at 15:00 UTC it has passed and rolls to tomorrow.
    before = daily_at("07:30", "America/Vancouver")(
        datetime(2026, 7, 10, 14, 0, tzinfo=UTC)
    )
    assert before == datetime(2026, 7, 10, 14, 30, tzinfo=UTC)

    after = daily_at("07:30", "America/Vancouver")(
        datetime(2026, 7, 10, 15, 0, tzinfo=UTC)
    )
    assert after == datetime(2026, 7, 11, 14, 30, tzinfo=UTC)


def test_daily_at_crosses_utc_day_boundary():
    # 2026-07-10 16:00 UTC is already 01:00 on the 11th in Tokyo (UTC+9), so
    # the next 23:30 Tokyo is later that same local day — 14:30 UTC on the 11th.
    nxt = daily_at("23:30", "Asia/Tokyo")(datetime(2026, 7, 10, 16, 0, tzinfo=UTC))
    assert nxt == datetime(2026, 7, 11, 14, 30, tzinfo=UTC)


def test_every_adds_interval():
    now = datetime(2026, 7, 10, 5, 0, tzinfo=UTC)
    assert every(hours=6)(now) == now + timedelta(hours=6)
    assert every(minutes=30)(now) == now + timedelta(minutes=30)


# ---------------------------------------------------------------------------
# run_pending
# ---------------------------------------------------------------------------


def _counter_job(name, next_run_fn, calls):
    return Job(name, next_run_fn, lambda: calls.append(name))


def test_first_tick_schedules_without_firing():
    calls: list[str] = []
    s = Scheduler([_counter_job("j", every(hours=1), calls)])
    t0 = datetime(2026, 7, 10, 5, 0, tzinfo=UTC)

    assert s.run_pending(t0) == []
    assert calls == []
    assert s.next_run("j") == t0 + timedelta(hours=1)


def test_fires_exactly_when_due_and_reschedules():
    calls: list[str] = []
    s = Scheduler([_counter_job("j", every(hours=1), calls)])
    t0 = datetime(2026, 7, 10, 5, 0, tzinfo=UTC)

    s.run_pending(t0)                                  # schedule
    assert s.run_pending(t0 + timedelta(minutes=59)) == []
    assert s.run_pending(t0 + timedelta(hours=1)) == ["j"]
    assert calls == ["j"]
    # rescheduled relative to the firing tick
    assert s.next_run("j") == t0 + timedelta(hours=2)


def test_fires_once_per_due_period_not_per_tick():
    calls: list[str] = []
    s = Scheduler([_counter_job("j", every(hours=1), calls)])
    t0 = datetime(2026, 7, 10, 5, 0, tzinfo=UTC)
    s.run_pending(t0)
    s.run_pending(t0 + timedelta(hours=1))
    s.run_pending(t0 + timedelta(hours=1, seconds=30))  # same period
    assert calls == ["j"]


def test_failing_job_does_not_block_siblings():
    calls: list[str] = []

    def boom():
        raise RuntimeError("job exploded")

    s = Scheduler(
        [
            Job("bad", every(hours=1), boom),
            _counter_job("good", every(hours=1), calls),
        ]
    )
    t0 = datetime(2026, 7, 10, 5, 0, tzinfo=UTC)
    s.run_pending(t0)
    fired = s.run_pending(t0 + timedelta(hours=1))

    assert fired == ["bad", "good"]
    assert calls == ["good"]
    assert "RuntimeError" in s.last_error["bad"]
    # the failing job stays on cadence
    assert s.next_run("bad") == t0 + timedelta(hours=2)


def test_error_clears_after_successful_run():
    flag = {"fail": True}

    def sometimes():
        if flag["fail"]:
            raise RuntimeError("first run fails")

    s = Scheduler([Job("j", every(hours=1), sometimes)])
    t0 = datetime(2026, 7, 10, 5, 0, tzinfo=UTC)
    s.run_pending(t0)
    s.run_pending(t0 + timedelta(hours=1))
    assert "j" in s.last_error

    flag["fail"] = False
    s.run_pending(t0 + timedelta(hours=2))
    assert "j" not in s.last_error
