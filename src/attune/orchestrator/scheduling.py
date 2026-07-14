"""Scheduling conflict detection + hold-slot proposals (design 1.2, 1.4, 4.2).

Design 4.2 calls out "a scheduling graph" as one of the small, single-purpose
graphs. Conflict detection itself stays a plain read-only function
(`detect_conflict` — rung-1 "communicate" behavior, no interrupt to
checkpoint around).

The write side finally has its settled trigger (see `docs/decisions.md`,
"Calendar write actions", roadmap prompt 16): **a detected conflict** may
offer a *resolution hold*. `propose_free_slots` is the read-only math for
that offer — same-day gaps big enough to rebook the conflicted meeting
into. The offer itself rides the standard draft-approve graph
(`Action.CREATE_HOLD` at PROPOSE), and only human approval materializes a
hold. Invite accept/decline, rescheduling, and time negotiation remain
explicitly deferred — argue with the decisions entry, not this docstring.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from ..connectors.base import CalendarEvent, WorkspaceConnector

WORKDAY_START_HOUR = 8
WORKDAY_END_HOUR = 18


@dataclass
class ConflictResult:
    event: CalendarEvent
    conflicting_with: CalendarEvent


def detect_conflict(
    connector: WorkspaceConnector, event: CalendarEvent
) -> ConflictResult | None:
    """Check whether ``event`` overlaps in time with any other event on the
    same calendar.

    ``list_events`` is scoped to the deployment's own calendar, so any two
    overlapping events returned by it are inherently a conflict for that
    person — no cross-calendar reasoning needed. Returns ``None`` when no
    conflict is found (including when ``event`` itself is the only thing in
    the window).
    """
    nearby = connector.list_events(time_min=event.start, time_max=event.end)
    for other in nearby:
        if other.event_id == event.event_id:
            continue
        if _overlaps(event, other):
            return ConflictResult(event=event, conflicting_with=other)
    return None


def _overlaps(a: CalendarEvent, b: CalendarEvent) -> bool:
    return a.start < b.end and b.start < a.end


def propose_free_slots(
    connector: WorkspaceConnector,
    event: CalendarEvent,
    *,
    max_candidates: int = 2,
) -> list[tuple[datetime, datetime]]:
    """Same-day free slots big enough to rebook ``event`` into.

    Read-only math: scans the conflicted event's own day (workday hours, in
    the event's timezone) for gaps of at least the event's duration between
    the calendar's busy blocks. Returns up to ``max_candidates``
    ``(start, end)`` pairs sized exactly to the event, earliest first —
    empty when the day is packed (the caller falls back to notify-only).
    """
    duration = event.end - event.start
    if duration <= timedelta(0):
        return []

    tz = event.start.tzinfo
    day = event.start.date()
    window_start = datetime.combine(day, time(WORKDAY_START_HOUR), tzinfo=tz)
    window_end = datetime.combine(day, time(WORKDAY_END_HOUR), tzinfo=tz)

    busy = sorted(
        (
            (e.start, e.end)
            for e in connector.list_events(
                time_min=window_start, time_max=window_end
            )
        ),
        key=lambda pair: pair[0],
    )

    slots: list[tuple[datetime, datetime]] = []
    cursor = window_start
    for start, end in busy:
        if start - cursor >= duration:
            slots.append((cursor, cursor + duration))
            if len(slots) >= max_candidates:
                return slots
        cursor = max(cursor, end)
    if window_end - cursor >= duration:
        slots.append((cursor, cursor + duration))
    return slots[:max_candidates]
