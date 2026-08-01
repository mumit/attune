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
hold.

Phase 3 stage 2 (see `docs/decisions.md`, "Calendar writes ship") adds two
more consumers of the same read-only functions here, both living in
`dispatcher.py` rather than this module: a DECLINE_INVITE proposal (using
`detect_conflict`'s result as one of its deterministic reasons) and a
RESCHEDULE proposal for a conflict the principal organizes (reusing
`propose_free_slots` unchanged). Time negotiation with the other party
remains explicitly deferred — argue with the decisions entry, not this
docstring.
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
    attendees: "list[str] | None" = None,
) -> list[tuple[datetime, datetime]]:
    """Same-day free slots big enough to rebook ``event`` into.

    Read-only math: scans the conflicted event's own day (workday hours, in
    the event's timezone) for gaps of at least the event's duration between
    busy blocks. Returns up to ``max_candidates`` ``(start, end)`` pairs
    sized exactly to the event, earliest first — empty when the day is
    packed (the caller falls back to notify-only).

    ``attendees`` (build prompt 30, task 6.3): when supplied AND
    ``connector.supports_freebusy()`` is true, each candidate slot is also
    checked against every attendee's OWN busy blocks (via
    ``connector.free_busy``) — a slot free on the organizer's own primary
    calendar but busy for an attendee isn't actually bookable. Absent
    attendees, or a connector that doesn't support the freebusy query, this
    degrades to exactly today's primary-calendar-only behavior (back-compat
    — every existing caller that doesn't pass ``attendees`` is unaffected).
    A freebusy query failure is best-effort: logged, never lets one
    unreachable attendee block the whole find-time search.
    """
    duration = event.end - event.start
    if duration <= timedelta(0):
        return []

    tz = event.start.tzinfo
    day = event.start.date()
    window_start = datetime.combine(day, time(WORKDAY_START_HOUR), tzinfo=tz)
    window_end = datetime.combine(day, time(WORKDAY_END_HOUR), tzinfo=tz)

    busy_blocks = [
        (e.start, e.end)
        for e in connector.list_events(time_min=window_start, time_max=window_end)
    ]

    if attendees and connector.supports_freebusy():
        try:
            per_attendee = connector.free_busy(
                attendees, time_min=window_start, time_max=window_end
            )
        except Exception:  # noqa: BLE001 — best-effort, see docstring
            import logging

            logging.getLogger(__name__).warning(
                "cross-attendee free_busy query failed; falling back to "
                "the primary calendar only", exc_info=True,
            )
            per_attendee = {}
        for blocks in per_attendee.values():
            busy_blocks.extend(blocks)

    # A standard interval-merge sweep (sorted by start, cursor advances by
    # max(cursor, end)) handles overlapping busy blocks from MULTIPLE
    # sources (the organizer's own calendar plus every attendee's)
    # correctly without needing an explicit merge pass first.
    busy = sorted(busy_blocks, key=lambda pair: pair[0])

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
