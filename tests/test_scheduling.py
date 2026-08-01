"""Tests for orchestrator/scheduling.py — no live connector, a FakeConnector
stands in.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from attune.connectors.base import CalendarEvent
from attune.orchestrator.scheduling import ConflictResult, detect_conflict


class _FakeConnector:
    def __init__(self, events: list[CalendarEvent]):
        self._events = events

    def list_events(self, *, time_min, time_max):
        return self._events


def _event(event_id, start_offset_min, duration_min=30, summary="Meeting"):
    base = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    start = base + timedelta(minutes=start_offset_min)
    end = start + timedelta(minutes=duration_min)
    return CalendarEvent(event_id=event_id, summary=summary, start=start, end=end)


# ---------------------------------------------------------------------------
# detect_conflict — no conflict
# ---------------------------------------------------------------------------


def test_no_conflict_when_alone():
    event = _event("e1", 0)
    conn = _FakeConnector([event])
    assert detect_conflict(conn, event) is None


def test_no_conflict_when_adjacent_not_overlapping():
    event = _event("e1", 0, duration_min=30)  # 09:00-09:30
    other = _event("e2", 30, duration_min=30)  # 09:30-10:00, back-to-back
    conn = _FakeConnector([event, other])
    assert detect_conflict(conn, event) is None


def test_excludes_itself_from_conflict_check():
    event = _event("e1", 0)
    conn = _FakeConnector([event])  # only itself in the window
    assert detect_conflict(conn, event) is None


# ---------------------------------------------------------------------------
# detect_conflict — conflict found
# ---------------------------------------------------------------------------


def test_conflict_detected_on_full_overlap():
    event = _event("e1", 0, duration_min=60, summary="Client call")
    other = _event("e2", 15, duration_min=30, summary="Standup")
    conn = _FakeConnector([event, other])

    result = detect_conflict(conn, event)

    assert isinstance(result, ConflictResult)
    assert result.event.event_id == "e1"
    assert result.conflicting_with.event_id == "e2"


def test_conflict_detected_on_partial_overlap():
    event = _event("e1", 0, duration_min=30)   # 09:00-09:30
    other = _event("e2", 15, duration_min=30)  # 09:15-09:45
    conn = _FakeConnector([event, other])

    result = detect_conflict(conn, event)

    assert result is not None
    assert result.conflicting_with.event_id == "e2"


def test_conflict_result_carries_both_events():
    event = _event("e1", 0, summary="1:1 with Priya")
    other = _event("e2", 0, summary="All-hands")
    conn = _FakeConnector([event, other])

    result = detect_conflict(conn, event)

    assert result.event.summary == "1:1 with Priya"
    assert result.conflicting_with.summary == "All-hands"


def test_returns_first_conflict_when_multiple_overlaps():
    event = _event("e1", 0, duration_min=60)
    other1 = _event("e2", 10, duration_min=10)
    other2 = _event("e3", 20, duration_min=10)
    conn = _FakeConnector([event, other1, other2])

    result = detect_conflict(conn, event)

    assert result.conflicting_with.event_id == "e2"


# ---------------------------------------------------------------------------
# detect_conflict — window passed to list_events
# ---------------------------------------------------------------------------


def test_uses_event_start_and_end_as_window():
    calls = []

    class _RecordingConnector:
        def list_events(self, *, time_min, time_max):
            calls.append((time_min, time_max))
            return []

    event = _event("e1", 0, duration_min=45)
    detect_conflict(_RecordingConnector(), event)

    assert calls == [(event.start, event.end)]


# ---------------------------------------------------------------------------
# propose_free_slots (prompt 16): read-only same-day rebooking math
# ---------------------------------------------------------------------------

from attune.orchestrator.scheduling import propose_free_slots  # noqa: E402


def test_first_free_slot_after_busy_morning():
    # busy 09:00-10:00 and 10:00-11:00; a 30-min conflicted event should get
    # 08:00 (before the busy run) first, then 11:00.
    conflicted = _event("e1", 0, duration_min=30)          # 09:00-09:30
    busy = [_event("b1", 0, duration_min=60), _event("b2", 60, duration_min=60)]
    conn = _FakeConnector(busy)

    slots = propose_free_slots(conn, conflicted)

    assert len(slots) == 2
    assert slots[0][0].hour == 8 and slots[0][1].hour == 8
    assert (slots[0][1] - slots[0][0]) == timedelta(minutes=30)
    assert slots[1][0] == datetime(2026, 7, 10, 11, 0, tzinfo=timezone.utc)


def test_packed_day_returns_no_slots():
    conflicted = _event("e1", 0, duration_min=60)
    # one busy block covering the whole 08:00-18:00 workday
    wall = CalendarEvent(
        event_id="wall", summary="Offsite",
        start=datetime(2026, 7, 10, 8, 0, tzinfo=timezone.utc),
        end=datetime(2026, 7, 10, 18, 0, tzinfo=timezone.utc),
    )
    assert propose_free_slots(_FakeConnector([wall]), conflicted) == []


def test_back_to_back_day_finds_only_real_gaps():
    conflicted = _event("e1", 0, duration_min=60)  # needs a full hour
    busy = [
        CalendarEvent(event_id="b1", summary="a",
                      start=datetime(2026, 7, 10, 8, 0, tzinfo=timezone.utc),
                      end=datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)),
        CalendarEvent(event_id="b2", summary="b",
                      start=datetime(2026, 7, 10, 12, 30, tzinfo=timezone.utc),
                      end=datetime(2026, 7, 10, 17, 30, tzinfo=timezone.utc)),
    ]
    # the 12:00-12:30 gap is too small for an hour; only 17:30-18:00 is too
    # small as well -> no slots
    assert propose_free_slots(_FakeConnector(busy), conflicted) == []


def test_slots_capped_at_two():
    conflicted = _event("e1", 0, duration_min=30)
    conn = _FakeConnector([])  # totally free day: many possible slots
    slots = propose_free_slots(conn, conflicted)
    assert len(slots) <= 2


def test_zero_duration_event_yields_nothing():
    e = CalendarEvent(
        event_id="z", summary="weird",
        start=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
        end=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
    )
    assert propose_free_slots(_FakeConnector([]), e) == []


# ---------------------------------------------------------------------------
# propose_free_slots — cross-attendee find-time (build prompt 30, task 6.3)
# ---------------------------------------------------------------------------


class _FreeBusyConnector(_FakeConnector):
    """Extends _FakeConnector with a fake free_busy query."""

    def __init__(self, events, *, busy_by_email=None, supports=True):
        super().__init__(events)
        self._busy_by_email = busy_by_email or {}
        self._supports = supports
        self.free_busy_calls: list[dict] = []

    def supports_freebusy(self):
        return self._supports

    def free_busy(self, emails, *, time_min, time_max):
        self.free_busy_calls.append(
            {"emails": list(emails), "time_min": time_min, "time_max": time_max}
        )
        return {e: self._busy_by_email.get(e, []) for e in emails}


def test_attendees_absent_never_calls_free_busy():
    """Back-compat: no attendees passed -> connector.free_busy is never
    consulted, even when the connector supports it."""
    conn = _FreeBusyConnector([], busy_by_email={"a@x.com": []})
    conflicted = _event("e1", 0, duration_min=30)

    slots = propose_free_slots(conn, conflicted)

    assert len(slots) == 1  # unaffected by the attendee machinery
    assert conn.free_busy_calls == []


def test_connector_without_freebusy_support_falls_back_to_primary_only():
    """attendees passed, but the connector doesn't support the freebusy
    query -> degrades to primary-calendar-only search, never crashes."""
    conn = _FreeBusyConnector(
        [], busy_by_email={"a@x.com": [(
            datetime(2026, 7, 10, 8, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 10, 18, 0, tzinfo=timezone.utc),
        )]},
        supports=False,
    )
    conflicted = _event("e1", 0, duration_min=30)

    slots = propose_free_slots(conn, conflicted, attendees=["a@x.com"])

    assert len(slots) == 1  # attendee's (unreachable) busy day is ignored
    assert conn.free_busy_calls == []


def test_slot_free_on_primary_but_busy_for_attendee_is_excluded():
    """The whole point of task 6.3: a slot open on the organizer's own
    calendar but busy for an attendee must not be proposed."""
    conflicted = _event("e1", 0, duration_min=30)  # forces an 08:00 slot search
    # Attendee is busy 08:00-11:00 -- the primary-calendar-only search
    # would have offered 08:00 first; cross-attendee awareness should push
    # past it to the next real gap.
    conn = _FreeBusyConnector(
        [], busy_by_email={
            "attendee@x.com": [(
                datetime(2026, 7, 10, 8, 0, tzinfo=timezone.utc),
                datetime(2026, 7, 10, 11, 0, tzinfo=timezone.utc),
            )],
        },
    )

    slots_without_attendee = propose_free_slots(conn, conflicted)
    assert slots_without_attendee[0][0].hour == 8  # would have offered 08:00

    slots_with_attendee = propose_free_slots(
        conn, conflicted, attendees=["attendee@x.com"]
    )
    assert all(s[0].hour >= 11 for s in slots_with_attendee)
    assert conn.free_busy_calls == [{
        "emails": ["attendee@x.com"],
        "time_min": datetime(2026, 7, 10, 8, 0, tzinfo=timezone.utc),
        "time_max": datetime(2026, 7, 10, 18, 0, tzinfo=timezone.utc),
    }]


def test_free_busy_query_failure_falls_back_to_primary_only():
    """Best-effort: a raising free_busy must not break the whole find-time
    search — the caller (e.g. an unreachable attendee) still gets
    primary-calendar-only slots rather than a crash."""

    class _RaisingConnector(_FakeConnector):
        def supports_freebusy(self):
            return True

        def free_busy(self, emails, *, time_min, time_max):
            raise RuntimeError("boom")

    conflicted = _event("e1", 0, duration_min=30)
    conn = _RaisingConnector([])

    slots = propose_free_slots(conn, conflicted, attendees=["a@x.com"])

    assert len(slots) == 1


def test_multiple_attendees_busy_blocks_are_all_merged():
    conflicted = _event("e1", 0, duration_min=30)
    conn = _FreeBusyConnector(
        [], busy_by_email={
            "a@x.com": [(
                datetime(2026, 7, 10, 8, 0, tzinfo=timezone.utc),
                datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
            )],
            "b@x.com": [(
                datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
                datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc),
            )],
        },
    )

    slots = propose_free_slots(conn, conflicted, attendees=["a@x.com", "b@x.com"])

    assert all(s[0].hour >= 10 for s in slots)
