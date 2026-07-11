"""In-process scheduler (design 4.6's missing piece — roadmap prompt 05).

Until this existed, ``Runtime.run()`` started the pull loops and nothing
else: the Gmail/Chat/Calendar watch renewals were never invoked (Gmail
watches silently lapse within 7 days), the morning brief — the Phase-0
deliverable — was never posted, ``store.consolidate()`` never ran, and the
pending-approvals ignore-sweep had no caller. Every one of those is a
recurring job; this module is the recurrence.

Deliberately hand-rolled rather than APScheduler: four jobs on fixed
cadences don't justify a dependency, and a ~60-line scheduler with an
injected clock is fully deterministic under test, which APScheduler is not.

Shape: a :class:`Job` pairs a name, a ``next_run_fn(now) -> datetime``
(:func:`daily_at` and :func:`every` cover all current needs), and a zero-arg
action. :meth:`Scheduler.run_pending` fires due jobs, reschedules them, and
isolates failures — one failing job logs and never blocks its siblings. The
only threaded part is :meth:`Scheduler.run_loop`, a thin tick wrapper
(``pragma: no cover``, same precedent as the pull loops).

Jobs run in the main process and are outbound-only — no ports (rule 5), no
new cloud dependencies.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

DEFAULT_TICK_SECONDS = 30


@dataclass
class Job:
    name: str
    next_run_fn: Callable[[datetime], datetime]
    action: Callable[[], Any]


def daily_at(time_str: str, tz: str = "UTC") -> Callable[[datetime], datetime]:
    """Next occurrence of a local wall-clock time ("HH:MM" in an IANA tz),
    returned as an aware UTC datetime strictly after ``now``."""
    hour, minute = (int(p) for p in time_str.split(":"))
    zone = ZoneInfo(tz)

    def next_run(now: datetime) -> datetime:
        local_now = now.astimezone(zone)
        candidate = local_now.replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if candidate <= local_now:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)

    return next_run


def every(*, hours: float = 0, minutes: float = 0) -> Callable[[datetime], datetime]:
    """A fixed interval from ``now``."""
    delta = timedelta(hours=hours, minutes=minutes)

    def next_run(now: datetime) -> datetime:
        return now + delta

    return next_run


@dataclass
class Scheduler:
    """Deterministic under an injected clock: nothing here reads the wall
    clock unless ``run_pending``/``run_loop`` are called without ``now``."""

    jobs: list[Job] = field(default_factory=list)
    _next: dict[str, datetime] = field(default_factory=dict, repr=False)
    last_error: dict[str, str] = field(default_factory=dict, repr=False)

    def add(self, job: Job) -> None:
        self.jobs.append(job)

    def run_pending(self, now: datetime | None = None) -> list[str]:
        """Fire every due job once, reschedule it, and isolate failures.

        A job seen for the first time is scheduled (not fired) — startup
        work that must happen immediately belongs to the caller (see
        ``Runtime.run``'s startup renewals), not to a fire-on-boot rule that
        would make every restart repost the brief.

        Returns the names of jobs that fired (including ones that failed —
        the failure is logged and recorded in ``last_error``, and the job is
        rescheduled normally so one bad run never stops the cadence).
        """
        now = now or datetime.now(timezone.utc)
        fired: list[str] = []
        for job in self.jobs:
            due_at = self._next.get(job.name)
            if due_at is None:
                self._next[job.name] = job.next_run_fn(now)
                continue
            if now < due_at:
                continue
            fired.append(job.name)
            try:
                job.action()
                self.last_error.pop(job.name, None)
            except Exception as exc:  # noqa: BLE001 — isolation is the contract
                self.last_error[job.name] = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "scheduler job %s failed: %s", job.name, exc, exc_info=True
                )
            self._next[job.name] = job.next_run_fn(now)
        return fired

    def next_run(self, name: str) -> datetime | None:
        """When a job will next fire (None until the first run_pending tick)."""
        return self._next.get(name)

    def run_loop(
        self, tick_seconds: int = DEFAULT_TICK_SECONDS
    ) -> None:  # pragma: no cover - thin live loop, logic tested via run_pending
        while True:
            self.run_pending()
            time.sleep(tick_seconds)
