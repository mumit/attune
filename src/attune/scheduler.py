"""In-process scheduler (design 4.6's missing piece — roadmap prompt 05).

Until this existed, ``Runtime.run()`` started the pull loops and nothing
else: the Gmail/Chat/Calendar watch renewals were never invoked (Gmail
watches silently lapse within 7 days), the morning brief — the Phase-0
deliverable — was never posted, ``store.consolidate()`` never ran, and the
pending-approvals ignore-sweep had no caller. Every one of those is a
recurring job; this module is the recurrence.

Deliberately hand-rolled rather than APScheduler: a handful of jobs on fixed
cadences don't justify a dependency, and a scheduler with an injected clock
is fully deterministic under test, which APScheduler is not. Durability
(build prompt 32, task 4) is a STORAGE change to this same hand-rolled
design, not a library change — the reasoning above still holds.

Shape: a :class:`Job` pairs a name, a ``next_run_fn(now) -> datetime``
(:func:`daily_at` and :func:`every` cover all current needs), a zero-arg
action, and a :class:`CatchUp` policy. :meth:`Scheduler.run_pending` fires
due jobs, reschedules them, and isolates failures — one failing job logs and
never blocks its siblings. The only threaded part is
:meth:`Scheduler.run_loop`, a thin tick wrapper (``pragma: no cover``, same
precedent as the pull loops).

Durability (build prompt 32, task 4): ``Scheduler._next`` used to be a
plain in-memory dict — a job seen for the first time was only *scheduled*,
never fired, and a restart before a job's due time silently forgot it ever
existed, with no catch-up. An optional ``store`` (:class:`SchedulerStore`,
:class:`SqliteSchedulerStore` in production) persists each job's next-run,
last-run outcome, and last error, so a NEW process picks up exactly where
the last one left off:

- A job the store has NEVER seen (first run ever, any process) is
  scheduled fresh from ``now`` — never fired on first sight, unchanged from
  the original in-memory behavior.
- A job the store last scheduled for a time that has ALREADY PASSED (the
  process was down through it) fires according to its ``catch_up`` policy:
  :attr:`CatchUp.FIRE_ONCE` fires it exactly once right now — "post today's
  brief if today's hasn't been posted," never once per missed period, since
  the next occurrence is always computed fresh from ``now`` after firing,
  collapsing any number of missed periods into one catch-up run.
  :attr:`CatchUp.SKIP` silently reschedules to the next occurrence after
  ``now`` without firing — appropriate for tight, low-stakes cadences
  (a sweep a few minutes late is no loss) where an immediate catch-up fire
  on every restart would just be noise.

Jobs run in the main process and are outbound-only — no ports (rule 5), no
new cloud dependencies.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

DEFAULT_TICK_SECONDS = 30


class CatchUp(str, Enum):
    """Build prompt 32, task 4: what an overdue job does on the first
    ``run_pending`` tick that notices it (a persisted ``next_run`` from a
    PRIOR process that has already passed)."""

    FIRE_ONCE = "fire_once"   # run it now, exactly once, regardless of how overdue
    SKIP = "skip"             # silently reschedule from now; don't fire


@dataclass
class Job:
    name: str
    next_run_fn: Callable[[datetime], datetime]
    action: Callable[[], Any]
    # Build prompt 32, task 4: FIRE_ONCE is the useful default for daily/
    # weekly cadences (a missed brief/digest is worth catching up on once);
    # tight interval jobs opt into SKIP explicitly at the call site that
    # registers them (see runtime.py's job assembly).
    catch_up: CatchUp = CatchUp.FIRE_ONCE


@dataclass(frozen=True)
class JobStatus:
    """One job's durable record — what :meth:`attune status`/Doctor read to
    answer "is anything silently dead" without needing the live process."""

    name: str
    next_run: datetime | None = None
    last_run_at: datetime | None = None
    last_outcome: str | None = None       # "success" | "failure" | None
    last_error: str | None = None
    last_success_at: datetime | None = None


class SchedulerStore(Protocol):
    def load(self, name: str) -> JobStatus | None: ...

    def save(self, status: JobStatus) -> None: ...

    def all(self) -> list[JobStatus]: ...


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt is not None else None


def _parse_iso(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


_SCHEDULER_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduler_jobs (
    name TEXT PRIMARY KEY,
    next_run TEXT,
    last_run_at TEXT,
    last_outcome TEXT,
    last_error TEXT,
    last_success_at TEXT
)
"""


class SqliteSchedulerStore:
    """SQLite-backed :class:`SchedulerStore` — one row per job, the same
    lazy-init/WAL/owner-only-permissions discipline as
    ``orchestrator.ledger.SqliteDecisionLedger``."""

    def __init__(self, path: str):
        self._path = path

    def _connect(self) -> sqlite3.Connection:
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self._path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_SCHEDULER_SCHEMA)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass
        return conn

    def load(self, name: str) -> JobStatus | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT name, next_run, last_run_at, last_outcome, last_error, "
                "last_success_at FROM scheduler_jobs WHERE name = ?",
                (name,),
            ).fetchone()
        if row is None:
            return None
        return JobStatus(
            name=row[0], next_run=_parse_iso(row[1]), last_run_at=_parse_iso(row[2]),
            last_outcome=row[3], last_error=row[4], last_success_at=_parse_iso(row[5]),
        )

    def save(self, status: JobStatus) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scheduler_jobs
                    (name, next_run, last_run_at, last_outcome, last_error, last_success_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    next_run=excluded.next_run, last_run_at=excluded.last_run_at,
                    last_outcome=excluded.last_outcome, last_error=excluded.last_error,
                    last_success_at=excluded.last_success_at
                """,
                (
                    status.name, _iso(status.next_run), _iso(status.last_run_at),
                    status.last_outcome, status.last_error, _iso(status.last_success_at),
                ),
            )

    def all(self) -> list[JobStatus]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name, next_run, last_run_at, last_outcome, last_error, "
                "last_success_at FROM scheduler_jobs ORDER BY name"
            ).fetchall()
        return [
            JobStatus(
                name=r[0], next_run=_parse_iso(r[1]), last_run_at=_parse_iso(r[2]),
                last_outcome=r[3], last_error=r[4], last_success_at=_parse_iso(r[5]),
            )
            for r in rows
        ]


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
    # Build prompt 32, task 4: optional durable backing. ``None`` (every
    # pre-existing caller/test) is byte-identical to the old in-memory-only
    # behavior — a job seen for the first time this PROCESS is scheduled,
    # never fired, exactly as before.
    store: Any = None
    _last_success: dict[str, datetime] = field(default_factory=dict, repr=False)

    def add(self, job: Job) -> None:
        self.jobs.append(job)

    def run_pending(self, now: datetime | None = None) -> list[str]:
        """Fire every due job once, reschedule it, and isolate failures.

        A job seen for the first time ANYWHERE (no in-memory cache entry and
        no persisted record in ``store``) is scheduled (not fired) — startup
        work that must happen immediately belongs to the caller (see
        ``Runtime.run``'s startup renewals), not to a fire-on-boot rule that
        would make every restart repost the brief. A job the store already
        knows about, whose persisted ``next_run`` has already passed (this
        process started after a prior one stopped), fires or skips per its
        ``catch_up`` policy — see the module docstring.

        Returns the names of jobs that fired (including ones that failed —
        the failure is logged and recorded in ``last_error``, and the job is
        rescheduled normally so one bad run never stops the cadence).
        """
        now = now or datetime.now(timezone.utc)
        fired: list[str] = []
        for job in self.jobs:
            due_at = self._next.get(job.name)
            if due_at is None:
                due_at = self._bootstrap(job, now)
                if due_at is None:
                    continue  # scheduled fresh (or skipped past an overdue catch-up); never fires this tick
            if now < due_at:
                continue
            self._fire(job, now)
            fired.append(job.name)
        return fired

    def _bootstrap(self, job: Job, now: datetime) -> datetime | None:
        """Resolve ``job``'s due time the first time THIS process sees it —
        either from a persisted record (a prior process already ran it) or
        fresh (truly first-ever run). Returns the due time to fire against
        this tick, or ``None`` when nothing should fire this tick (freshly
        scheduled, or an overdue job whose ``catch_up`` policy skipped it)."""
        persisted = self.store.load(job.name) if self.store is not None else None
        if persisted is None or persisted.next_run is None:
            self._next[job.name] = job.next_run_fn(now)
            self._persist_next_only(job.name, self._next[job.name])
            return None
        if persisted.last_success_at is not None:
            self._last_success[job.name] = persisted.last_success_at
        if persisted.last_error:
            self.last_error[job.name] = persisted.last_error
        if persisted.next_run > now:
            self._next[job.name] = persisted.next_run
            return None
        # Overdue: a prior process scheduled this for a time that has since
        # passed while nothing was running.
        if job.catch_up is CatchUp.SKIP:
            self._next[job.name] = job.next_run_fn(now)
            self._persist_next_only(job.name, self._next[job.name])
            return None
        # FIRE_ONCE: treat it as due right now — exactly once, regardless of
        # how many periods were actually missed (the next occurrence below
        # is always computed fresh from `now`, collapsing any number of
        # missed periods into this one catch-up run).
        self._next[job.name] = persisted.next_run
        return persisted.next_run

    def _persist_next_only(self, name: str, next_run: datetime) -> None:
        if self.store is None:
            return
        self.store.save(JobStatus(
            name=name, next_run=next_run,
            last_run_at=None, last_outcome=None, last_error=self.last_error.get(name),
            last_success_at=self._last_success.get(name),
        ))

    def _fire(self, job: Job, now: datetime) -> None:
        outcome = "success"
        error: str | None = None
        try:
            job.action()
            self.last_error.pop(job.name, None)
            self._last_success[job.name] = now
        except Exception as exc:  # noqa: BLE001 — isolation is the contract
            outcome = "failure"
            error = f"{type(exc).__name__}: {exc}"
            self.last_error[job.name] = error
            logger.warning(
                "scheduler job %s failed: %s", job.name, exc, exc_info=True
            )
        self._next[job.name] = job.next_run_fn(now)
        if self.store is not None:
            self.store.save(JobStatus(
                name=job.name, next_run=self._next[job.name], last_run_at=now,
                last_outcome=outcome, last_error=error,
                last_success_at=self._last_success.get(job.name),
            ))

    def next_run(self, name: str) -> datetime | None:
        """When a job will next fire (None until the first run_pending tick)."""
        return self._next.get(name)

    def run_loop(
        self, tick_seconds: int = DEFAULT_TICK_SECONDS
    ) -> None:  # pragma: no cover - thin live loop, logic tested via run_pending
        while True:
            self.run_pending()
            time.sleep(tick_seconds)
