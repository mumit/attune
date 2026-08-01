"""Tests for orchestrator/routines.py — user-authored recurring routines
(build prompt 32, task 1).

Covers the acceptance criteria:
- three routines, each firing at the right time under an injected clock;
- a routine requesting something outside the planner vocabulary (WRITE or
  GENERAL) is refused at creation time with a clear error;
- the default brief routine exists after first touch, and removing it stops
  the brief (covered at the runtime.build_scheduler layer in
  test_runtime.py's test_build_scheduler_assembles_expected_jobs and a
  dedicated test below).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from attune.orchestrator.routines import (
    DEFAULT_ROUTINE_NAME,
    DEFAULT_ROUTINE_REQUEST,
    JsonRoutineStore,
    Routine,
    RoutineError,
    open_routine_store,
    parse_schedule,
    validate_routine_request,
)

UTC = timezone.utc


class _Client:
    """Mirrors test_interaction.py's fake — a fixed four-line planner reply."""

    def __init__(self, reply: str):
        self.reply = reply

    def chat_completions_create(self, **kwargs):
        class _Message:
            content = self.reply

        class _Choice:
            message = _Message()

        class _Response:
            choices = [_Choice()]

        return _Response()


def _reply(intent: str, *, gmail_query="NONE", start="NONE", end="NONE"):
    return f"INTENT: {intent}\nGMAIL_QUERY: {gmail_query}\nSTART: {start}\nEND: {end}"


# ---------------------------------------------------------------------------
# validate_routine_request — the bounded vocabulary gate
# ---------------------------------------------------------------------------


def test_validate_accepts_brief():
    plan = validate_routine_request(_Client(_reply("BRIEF")), "give me the morning brief")
    assert plan.intent.value == "brief"


def test_validate_accepts_mail():
    plan = validate_routine_request(
        _Client(_reply("MAIL", gmail_query="is:unread from:hightier@x.com")),
        "unresolved threads from HIGH-tier senders",
    )
    assert plan.intent.value == "mail"


def test_validate_accepts_calendar():
    plan = validate_routine_request(
        _Client(_reply(
            "CALENDAR", start="2026-07-20T00:00:00+00:00", end="2026-07-21T00:00:00+00:00",
        )),
        "today's conflicts",
    )
    assert plan.intent.value == "calendar"


def test_validate_refuses_write_at_creation_time():
    """A routine requesting a mutation is refused with a clear error — a
    routine is never a grant."""
    with pytest.raises(RoutineError, match="never a grant"):
        validate_routine_request(_Client(_reply("WRITE")), "send a reply to my boss every morning")


def test_validate_refuses_general_at_creation_time():
    with pytest.raises(RoutineError, match="bounded request"):
        validate_routine_request(_Client(_reply("GENERAL")), "tell me a joke")


# ---------------------------------------------------------------------------
# parse_schedule — the bounded schedule vocabulary
# ---------------------------------------------------------------------------


def test_parse_schedule_daily():
    next_run = parse_schedule("daily 07:30")
    assert next_run(datetime(2026, 7, 10, 5, 0, tzinfo=UTC)) == datetime(2026, 7, 10, 7, 30, tzinfo=UTC)


def test_parse_schedule_weekday_skips_weekend():
    # 2026-07-10 is a Friday.
    next_run = parse_schedule("weekday 08:00")
    after_friday_run = next_run(datetime(2026, 7, 10, 9, 0, tzinfo=UTC))
    assert after_friday_run == datetime(2026, 7, 13, 8, 0, tzinfo=UTC)  # Monday


def test_parse_schedule_weekend():
    next_run = parse_schedule("weekend 09:00")
    # Friday morning -> next Saturday.
    assert next_run(datetime(2026, 7, 10, 5, 0, tzinfo=UTC)) == datetime(2026, 7, 11, 9, 0, tzinfo=UTC)


def test_parse_schedule_explicit_day_list():
    next_run = parse_schedule("mon,wed,fri 14:00")
    # Friday 2026-07-10 before 14:00 -> same day.
    assert next_run(datetime(2026, 7, 10, 5, 0, tzinfo=UTC)) == datetime(2026, 7, 10, 14, 0, tzinfo=UTC)
    # Friday after 14:00 -> next Monday.
    assert next_run(datetime(2026, 7, 10, 15, 0, tzinfo=UTC)) == datetime(2026, 7, 13, 14, 0, tzinfo=UTC)


@pytest.mark.parametrize("spec", ["bogus 08:00", "daily 25:00", "daily 08", "weekday", ""])
def test_parse_schedule_refuses_malformed_specs(spec):
    with pytest.raises(RoutineError):
        parse_schedule(spec)


# ---------------------------------------------------------------------------
# JsonRoutineStore / open_routine_store
# ---------------------------------------------------------------------------


def test_store_add_get_list_remove_round_trip(tmp_path):
    store = JsonRoutineStore(str(tmp_path / "routines.json"))
    r = Routine(
        name="hightier", request="unresolved threads from HIGH-tier senders",
        schedule="weekday 08:00", created_at=datetime(2026, 7, 10, tzinfo=UTC),
    )
    store.add(r)

    assert store.get("hightier") == r
    assert store.list() == [r]
    assert store.remove("hightier") is True
    assert store.get("hightier") is None
    assert store.remove("hightier") is False


def test_open_routine_store_seeds_default_on_first_touch(tmp_path):
    path = str(tmp_path / "routines.json")
    store = open_routine_store(path, brief_time="07:30")

    routine = store.get(DEFAULT_ROUTINE_NAME)
    assert routine is not None
    assert routine.request == DEFAULT_ROUTINE_REQUEST
    assert routine.schedule == "daily 07:30"


def test_open_routine_store_does_not_reseed_after_removal(tmp_path):
    """Once explicitly removed, the default routine stays gone — the store
    file now exists, so it is never re-seeded."""
    path = str(tmp_path / "routines.json")
    store = open_routine_store(path, brief_time="07:30")
    store.remove(DEFAULT_ROUTINE_NAME)

    reopened = open_routine_store(path, brief_time="07:30")
    assert reopened.get(DEFAULT_ROUTINE_NAME) is None
    assert reopened.list() == []


# ---------------------------------------------------------------------------
# Acceptance: three routines, each firing at the right time under an
# injected clock.
# ---------------------------------------------------------------------------


def test_three_routines_each_fire_at_the_right_time_under_injected_clock():
    from attune.scheduler import Job, Scheduler

    calls: list[str] = []
    scheduler = Scheduler([
        Job("routine:morning_brief", parse_schedule("daily 07:30"), lambda: calls.append("morning_brief")),
        Job("routine:hightier", parse_schedule("weekday 08:00"), lambda: calls.append("hightier")),
        Job("routine:weekend_digest", parse_schedule("weekend 09:00"), lambda: calls.append("weekend_digest")),
    ])

    # Friday 2026-07-10, 06:00 UTC: nothing has fired yet.
    t0 = datetime(2026, 7, 10, 6, 0, tzinfo=UTC)
    assert scheduler.run_pending(t0) == []
    assert calls == []

    # 07:30 — only the daily brief is due.
    assert scheduler.run_pending(datetime(2026, 7, 10, 7, 30, tzinfo=UTC)) == ["routine:morning_brief"]
    # 08:00 — the weekday routine is now due too (Friday is a weekday).
    assert scheduler.run_pending(datetime(2026, 7, 10, 8, 0, tzinfo=UTC)) == ["routine:hightier"]
    # Still Friday, 09:00 — the weekend routine is NOT due (not a weekend day).
    assert scheduler.run_pending(datetime(2026, 7, 10, 9, 0, tzinfo=UTC)) == []
    # Saturday 2026-07-11, 09:00 — the weekend routine fires (the daily
    # brief's own 07:30 Saturday occurrence has also passed by now and
    # fires alongside it — that's expected, independent, cadence, not a bug).
    fired = scheduler.run_pending(datetime(2026, 7, 11, 9, 0, tzinfo=UTC))
    assert "routine:weekend_digest" in fired

    assert calls == ["morning_brief", "hightier", "morning_brief", "weekend_digest"]
