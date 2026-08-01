"""User-authored recurring routines (build prompt 32, task 1).

Three major vendors converged on the same finding within months of each
other: OpenAI retired Pulse's fixed-cadence push (2026-06-17) for
user-authored Scheduled Tasks, Google shipped Scheduled Actions, Anthropic
shipped scheduled tasks inside Cowork. Proactive experiences work when they
are *personalised, action-oriented, and steerable by the user* — not a
fixed daily broadcast the principal never asked for and can't change.

A routine is a stored, named, scheduled REQUEST — never a tool loop, never
free-form instructions executed unattended. ``validate_routine_request``
parses it through the exact same bounded planner every Slack/Chat DM
already goes through (:func:`interaction.plan_interaction`) and refuses, at
creation time, anything a live DM to Attune could not already express:

- ``WRITE`` — a mutation. A routine is never a grant (constraint, build
  prompt 32): the planner can classify a write request, but nothing
  downstream may ever execute one unattended, so accepting it into a
  routine would be a silent lie about what will happen at 8am tomorrow.
- ``GENERAL`` — no recognized bounded read. Free-form conversation needs a
  human in the loop to steer it; there is nothing for an unattended job to
  execute.

``BRIEF``, ``MAIL``, and ``CALENDAR`` are the only intents a routine may
ever store. The routine's REQUEST TEXT is what's persisted — the plan
itself is deliberately re-derived fresh at every firing (see
``dispatcher.run_routine``), not cached from creation time, so a routine
asking about "today's conflicts" resolves "today" against the day it
actually runs, not the day it was authored.

The daily brief ships as one default routine (``DEFAULT_ROUTINE_NAME``),
seeded the first time the routine store is ever touched — see
:func:`open_routine_store`. The brief stops being the architecture and
becomes a default: visible via ``attune routine list``, editable, and
removable like anything else a principal authors.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

from ..fslock import locked
from ..interaction import InteractionIntent, InteractionPlan, plan_interaction

DEFAULT_ROUTINE_NAME = "morning_brief"
DEFAULT_ROUTINE_REQUEST = "give me the morning brief"

# The bounded schedule vocabulary a routine may be authored with — a day
# selector (a named group, or an explicit comma-separated weekday list) plus
# an "HH:MM" wall-clock time, e.g. "weekday 08:00", "daily 07:30",
# "mon,wed,fri 14:00".
_DAY_GROUPS = {
    "daily": frozenset(range(7)),
    "weekday": frozenset({0, 1, 2, 3, 4}),
    "weekend": frozenset({5, 6}),
}
_DAY_ALIASES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_SCHEDULE_RE = re.compile(r"^(\S+)\s+(\d{1,2}):(\d{2})$")


class RoutineError(Exception):
    """A routine request or schedule is invalid — refused at creation time,
    never at firing time (see the module docstring's vocabulary rule)."""


@dataclass(frozen=True)
class Routine:
    name: str
    request: str
    schedule: str
    created_at: datetime


def validate_routine_request(
    client: Any,
    request: str,
    *,
    timezone_name: str = "UTC",
    now: datetime | None = None,
) -> InteractionPlan:
    """Parse ``request`` through the same bounded planner a live DM uses,
    refusing (:class:`RoutineError`) anything outside a routine's allowed
    vocabulary (see module docstring). Returns the validated plan — callers
    that only need to validate (``attune routine add``) can discard it;
    ``dispatcher.run_routine`` re-derives its own plan fresh at firing time
    regardless, so a cached plan here is never relied on for execution.
    """
    plan = plan_interaction(client, request, timezone_name=timezone_name, now=now)
    if plan.intent == InteractionIntent.WRITE:
        raise RoutineError(
            f"{request!r} reads as a request to change Workspace data — "
            "routines are read-only proactive messages, never a grant. "
            "Rephrase as a read (a brief, a mail search, or a calendar "
            "question)."
        )
    if plan.intent == InteractionIntent.GENERAL:
        raise RoutineError(
            f"{request!r} didn't resolve to a specific bounded request "
            "(brief, mail, or calendar) — a routine can only ask for what a "
            "DM to Attune could already answer unattended. Try naming what "
            "you want checked, e.g. 'unresolved threads from HIGH-tier "
            "senders'."
        )
    return plan


def parse_schedule(spec: str, tz: str = "UTC") -> Callable[[datetime], datetime]:
    """Compile a routine's ``"<days> HH:MM"`` schedule into a
    ``scheduler.Job``-compatible ``next_run_fn`` — the same shape
    ``scheduler.daily_at`` already produces, generalized with a day
    selector. Raises :class:`RoutineError` (never a bare parse exception)
    on anything malformed, so ``attune routine add`` reports a clear error
    at creation time rather than a stack trace at the next scheduler tick.
    """
    match = _SCHEDULE_RE.match(spec.strip())
    if not match:
        raise RoutineError(
            f"invalid schedule {spec!r} — expected '<days> HH:MM', e.g. "
            "'weekday 08:00' (days: daily, weekday, weekend, or a "
            "comma-separated list like mon,wed,fri)"
        )
    days_spec, hour_s, minute_s = match.groups()
    hour, minute = int(hour_s), int(minute_s)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise RoutineError(f"invalid time in schedule {spec!r} — HH:MM out of range")

    if days_spec in _DAY_GROUPS:
        allowed = _DAY_GROUPS[days_spec]
    else:
        try:
            allowed = frozenset(
                _DAY_ALIASES[d.strip().lower()] for d in days_spec.split(",")
            )
        except KeyError as exc:
            raise RoutineError(
                f"unknown day {exc.args[0]!r} in schedule {spec!r} — use "
                "daily, weekday, weekend, or mon/tue/wed/thu/fri/sat/sun"
            ) from exc
        if not allowed:
            raise RoutineError(f"schedule {spec!r} names no days")

    zone = ZoneInfo(tz)

    def next_run(now: datetime) -> datetime:
        local_now = now.astimezone(zone)
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local_now:
            candidate += timedelta(days=1)
        for _ in range(8):  # at most one full week to find a matching day
            if candidate.weekday() in allowed:
                return candidate.astimezone(timezone.utc)
            candidate += timedelta(days=1)
        raise RoutineError(f"schedule {spec!r} never matches any day")  # pragma: no cover - unreachable, allowed is non-empty

    return next_run


class RoutineStore(Protocol):
    def list(self) -> list[Routine]: ...

    def get(self, name: str) -> Routine | None: ...

    def add(self, routine: Routine) -> None: ...

    def remove(self, name: str) -> bool:
        """True if a routine named ``name`` existed and was removed."""
        ...

    def exists(self) -> bool:
        """Whether the underlying store has ever been written — distinct
        from ``list() == []``, which is also true right after the LAST
        routine is explicitly removed. See :func:`open_routine_store`."""
        ...


class JsonRoutineStore:
    """File-backed registry: ``{name: {request, schedule, created_at}}`` —
    same read-fully/rewrite-fully shape as ``orchestrator/pending.py``,
    fine at single-principal scale."""

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.RLock()

    def exists(self) -> bool:
        return os.path.exists(self._path)

    def list(self) -> list[Routine]:
        with self._lock, locked(self._path + ".lock"):
            data = self._load()
        return [self._routine_from_raw(name, raw) for name, raw in data.items()]

    def get(self, name: str) -> Routine | None:
        with self._lock, locked(self._path + ".lock"):
            data = self._load()
        raw = data.get(name)
        return self._routine_from_raw(name, raw) if raw is not None else None

    def add(self, routine: Routine) -> None:
        with self._lock, locked(self._path + ".lock"):
            data = self._load()
            data[routine.name] = {
                "request": routine.request,
                "schedule": routine.schedule,
                "created_at": routine.created_at.astimezone(timezone.utc).isoformat(),
            }
            self._save(data)

    def remove(self, name: str) -> bool:
        with self._lock, locked(self._path + ".lock"):
            data = self._load()
            if name not in data:
                return False
            del data[name]
            self._save(data)
            return True

    @staticmethod
    def _routine_from_raw(name: str, raw: dict[str, Any]) -> Routine:
        return Routine(
            name=name,
            request=raw["request"],
            schedule=raw["schedule"],
            created_at=datetime.fromisoformat(raw["created_at"]),
        )

    def _load(self) -> dict[str, Any]:
        if not os.path.exists(self._path):
            return {}
        with open(self._path) as fh:
            return json.load(fh)

    def _save(self, data: dict[str, Any]) -> None:
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        temp = f"{self._path}.tmp"
        with open(temp, "w") as fh:
            json.dump(data, fh)
        os.chmod(temp, 0o600)
        os.replace(temp, self._path)


def open_routine_store(
    path: str, *, brief_time: str = "07:30", now: datetime | None = None,
) -> JsonRoutineStore:
    """The one entry point every caller (CLI, ``runtime.build_runtime``)
    uses to reach the routine store — seeds :data:`DEFAULT_ROUTINE_NAME`
    the FIRST TIME the underlying file has never existed at all, never on
    an empty-but-existing store. This is what makes "the default brief
    routine exists after init" true for a fresh deployment on first touch
    (no separate ``attune init`` wiring needed — the exact same request
    text every direct DM "brief" keyword already resolves to
    deterministically, no live model call required) while a deliberate
    ``attune routine remove morning_brief`` stays removed forever (the file
    exists from that point on, so it is never re-seeded).
    """
    store = JsonRoutineStore(path)
    if not store.exists():
        store.add(Routine(
            name=DEFAULT_ROUTINE_NAME,
            request=DEFAULT_ROUTINE_REQUEST,
            schedule=f"daily {brief_time}",
            created_at=now or datetime.now(timezone.utc),
        ))
    return store
