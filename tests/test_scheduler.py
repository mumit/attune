"""Tests for scheduler.py — deterministic under an injected clock, no threads
(the run_loop shell is pragma: no cover; everything it calls is tested here).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from attune.scheduler import (
    CatchUp,
    Job,
    Scheduler,
    SqliteSchedulerStore,
    daily_at,
    every,
)

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


# ---------------------------------------------------------------------------
# Durability + catch-up semantics (build prompt 32, task 4)
# ---------------------------------------------------------------------------


def test_first_run_ever_schedules_without_firing_even_with_a_store(tmp_path):
    """A job the store has NEVER seen behaves exactly like the old
    in-memory-only scheduler: scheduled fresh, never fired."""
    calls: list[str] = []
    store = SqliteSchedulerStore(str(tmp_path / "scheduler.db"))
    s = Scheduler([_counter_job("daily_brief", daily_at("07:30"), calls)], store=store)
    t0 = datetime(2026, 7, 10, 5, 0, tzinfo=UTC)

    assert s.run_pending(t0) == []
    assert calls == []
    assert store.load("daily_brief").next_run == datetime(2026, 7, 10, 7, 30, tzinfo=UTC)


def test_fire_once_catchup_after_a_missed_window_fires_exactly_once(tmp_path):
    """A restart after a missed brief window posts exactly one brief."""
    calls: list[str] = []
    db_path = str(tmp_path / "scheduler.db")
    t0 = datetime(2026, 7, 10, 5, 0, tzinfo=UTC)

    # Process 1: schedules the job for 07:30, then goes down before firing.
    store1 = SqliteSchedulerStore(db_path)
    s1 = Scheduler(
        [Job("daily_brief", daily_at("07:30"), lambda: calls.append("daily_brief"),
             catch_up=CatchUp.FIRE_ONCE)],
        store=store1,
    )
    s1.run_pending(t0)
    assert calls == []

    # Process 2 (a fresh Scheduler instance, simulating a restart) starts up
    # well past the missed 07:30 window.
    store2 = SqliteSchedulerStore(db_path)
    s2 = Scheduler(
        [Job("daily_brief", daily_at("07:30"), lambda: calls.append("daily_brief"),
             catch_up=CatchUp.FIRE_ONCE)],
        store=store2,
    )
    restart_now = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    fired = s2.run_pending(restart_now)

    assert fired == ["daily_brief"]
    assert calls == ["daily_brief"]
    # rescheduled for tomorrow, from the restart time forward — not "every
    # missed occurrence since 07:30".
    assert s2.next_run("daily_brief") == datetime(2026, 7, 11, 7, 30, tzinfo=UTC)

    # A second tick shortly after does NOT fire again.
    s2.run_pending(restart_now + timedelta(minutes=5))
    assert calls == ["daily_brief"]


def test_fire_once_catchup_after_three_missed_windows_still_fires_exactly_once(tmp_path):
    calls: list[str] = []
    db_path = str(tmp_path / "scheduler.db")
    t0 = datetime(2026, 7, 10, 5, 0, tzinfo=UTC)

    store1 = SqliteSchedulerStore(db_path)
    Scheduler(
        [Job("daily_brief", daily_at("07:30"), lambda: calls.append("x"))],
        store=store1,
    ).run_pending(t0)

    # Three days pass with the process down the whole time.
    store2 = SqliteSchedulerStore(db_path)
    restart_now = datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
    fired = Scheduler(
        [Job("daily_brief", daily_at("07:30"), lambda: calls.append("x"))],
        store=store2,
    ).run_pending(restart_now)

    assert fired == ["daily_brief"]
    assert calls == ["x"]  # exactly once, not three times


def test_skip_catchup_reschedules_silently_without_firing(tmp_path):
    calls: list[str] = []
    db_path = str(tmp_path / "scheduler.db")
    t0 = datetime(2026, 7, 10, 5, 0, tzinfo=UTC)

    store1 = SqliteSchedulerStore(db_path)
    Scheduler(
        [Job("sweep", every(minutes=5), lambda: calls.append("x"), catch_up=CatchUp.SKIP)],
        store=store1,
    ).run_pending(t0)

    store2 = SqliteSchedulerStore(db_path)
    restart_now = t0 + timedelta(hours=6)  # long overdue
    fired = Scheduler(
        [Job("sweep", every(minutes=5), lambda: calls.append("x"), catch_up=CatchUp.SKIP)],
        store=store2,
    ).run_pending(restart_now)

    assert fired == []
    assert calls == []
    assert Scheduler(
        [Job("sweep", every(minutes=5), lambda: calls.append("x"))], store=store2,
    ).next_run("sweep") is None  # not yet re-bootstrapped in a THIRD instance
    assert store2.load("sweep").next_run == restart_now + timedelta(minutes=5)


def test_last_error_and_last_success_persist_and_reload(tmp_path):
    db_path = str(tmp_path / "scheduler.db")
    t0 = datetime(2026, 7, 10, 5, 0, tzinfo=UTC)

    def boom():
        raise RuntimeError("kaboom")

    store1 = SqliteSchedulerStore(db_path)
    s1 = Scheduler([Job("bad", every(hours=1), boom)], store=store1)
    s1.run_pending(t0)
    s1.run_pending(t0 + timedelta(hours=1))

    status = store1.load("bad")
    assert status.last_outcome == "failure"
    assert "RuntimeError" in status.last_error
    assert status.last_run_at == t0 + timedelta(hours=1)

    # A fresh Scheduler instance (new process) reloads the error without
    # needing to re-run the job.
    store2 = SqliteSchedulerStore(db_path)
    s2 = Scheduler([Job("bad", every(hours=1), boom)], store=store2)
    s2.run_pending(t0 + timedelta(hours=1, minutes=1))  # not yet due again
    assert s2.last_error["bad"]


def test_sqlite_scheduler_store_round_trips_all_fields(tmp_path):
    from attune.scheduler import JobStatus

    store = SqliteSchedulerStore(str(tmp_path / "scheduler.db"))
    ts = datetime(2026, 7, 10, 7, 30, tzinfo=UTC)
    store.save(JobStatus(
        name="j", next_run=ts, last_run_at=ts, last_outcome="success",
        last_error=None, last_success_at=ts,
    ))
    loaded = store.load("j")
    assert loaded == JobStatus(
        name="j", next_run=ts, last_run_at=ts, last_outcome="success",
        last_error=None, last_success_at=ts,
    )
    assert store.load("missing") is None
    assert [s.name for s in store.all()] == ["j"]
