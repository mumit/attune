"""Pending-approval tracking (design 2.2's IGNORED signal + card hygiene).

Three jobs, all about what happens *around* an approval card rather than in it:

1. **Dedupe.** Every Gmail notification that touches a thread starts a fresh
   draft-approve workflow and posts a fresh card. Without tracking, two quick
   replies on one thread mean two live cards, one of them stale. The registry
   lets ``dispatcher.handle_gmail_notification`` skip threads that already
   have a pending card.

2. **The IGNORED signal.** ``memory/signals.py`` defines
   ``ActionSignal.IGNORED`` ("left untouched → weak negative") — design 2.2
   calls it one of the two most underused capture signals — but nothing ever
   tracked whether a card was acted on, so it could never fire.
   :func:`sweep_ignored` turns cards pending longer than a threshold into
   IGNORED captures (called on a schedule; see the scheduler).

3. **Expiry (build prompt 31, task 3).** ``sweep_ignored`` alone left a real
   gap: an entry it marks IGNORED still has a live, resumable workflow behind
   it forever — a click six months later still resumes, protected only by
   the apply-time freshness check. :func:`sweep_expired` is the harder stop:
   past :data:`DEFAULT_EXPIRY` (7 days — see the function's own docstring for
   why), an entry (PENDING *or* IGNORED) is marked ``STATUS_EXPIRED`` and its
   underlying LangGraph checkpoint is actually deleted (``checkpointer.
   delete_thread``, threaded through as ``cancel_workflow``) — not just
   flagged. A later click on an expired thread_id has no checkpoint to
   resume against at all; ``resume_workflow`` checks the registry FIRST and
   returns an honest refusal rather than ever reaching the graph.

   EXPIRED is deliberately a distinct status from IGNORED, both in this
   registry and in the ledger/learning signal each produces (see
   :func:`sweep_expired`'s own docstring): IGNORED means "the principal saw
   this and did nothing — weak negative signal about the proposal." EXPIRED
   means "so much time passed the workflow itself is dead" — a fact about
   elapsed time, not a judgment about the proposal, and conflating the two
   would corrupt ``grants.track_records``' evidence.

``PendingApprovals`` is a Protocol with a JSON-file-backed implementation,
same shape as ``ingestion/state.py``: read fully, rewrite fully, fine at
single-mailbox scale.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from ..fslock import locked
from ..memory.base import MemoryStore
from ..memory.signals import ActionSignal, capture_action_signal

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_RESOLVED = "resolved"   # a human answered the card
STATUS_IGNORED = "ignored"     # the sweep marked it stale (still resumable)
# Build prompt 31, task 3: distinct from STATUS_IGNORED — the underlying
# workflow was actually cancelled (checkpoint deleted), never resumable
# again, regardless of a later click. See the module docstring.
STATUS_EXPIRED = "expired"

# The approval TTL (build prompt 31, task 3): how long a card may sit
# PENDING or IGNORED before its workflow is cancelled outright. 7 days,
# not hosted's 15-minute APPROVAL_LIFETIME (hosted/capability_admission.py)
# — a person's approval channel is not a request/response cycle the way a
# dispatched job is. 7 days covers a full workweek plus a weekend (a card
# posted Friday evening is still live Monday morning) while still being a
# real, human-legible ceiling — "ask me again" after a week is honest;
# resuming a six-month-old click, the status quo this build prompt fixes,
# is not. See docs/decisions.md for the recorded justification.
DEFAULT_EXPIRY = timedelta(days=7)


@dataclass
class PendingApproval:
    lg_tid: str          # the LangGraph workflow thread id
    source_ref: str      # what the card is about (e.g. the Gmail thread id)
    domain: str
    posted_at: datetime  # UTC
    status: str = STATUS_PENDING
    # The thread's counterparty (mail: thread.from_addr; calendar: organizer,
    # or None — CalendarEvent carries no organizer field today) — feeds the
    # importance profile when the sweep captures an IGNORED signal (Phase 1,
    # G5/G6). Optional and backward compatible: entries persisted before this
    # field existed simply parse back with sender=None, so an old JSON file
    # keeps working, it just can't feed the profile.
    sender: str | None = None
    # thread.subject / event.summary — the discriminating topic
    # sweep_ignored needs to write a real, retrievable summary instead of a
    # raw thread/event id (build prompt 25, task 1). Same back-compat
    # posture as ``sender``: absent on entries persisted before this field
    # existed, parsing back as None.
    subject: str | None = None
    # The effective triage.Priority value at posting time (mail only; no
    # analogous model classification for a deterministic calendar proposal)
    # — feeds the same capture text as ``subject`` above.
    priority: str | None = None
    # The autonomy.Action value this card proposes (e.g. "draft_reply",
    # "label", "decline_invite") — build prompt 31, task 4: batch cards
    # group several pending entries by (domain, action), which needs this
    # field to exist at all. Same back-compat posture as sender/subject/
    # priority: absent on entries persisted before this field existed,
    # parsing back as None (such an entry simply never joins a batch).
    action: str | None = None


class PendingApprovals(Protocol):
    def get_pending_for_source(self, source_ref: str) -> PendingApproval | None:
        """The pending entry for a source item, or None."""
        ...

    def register(
        self,
        *,
        lg_tid: str,
        source_ref: str,
        domain: str,
        posted_at: datetime,
        sender: str | None = None,
        subject: str | None = None,
        priority: str | None = None,
        action: str | None = None,
    ) -> None:
        """Record a newly posted approval card as pending."""
        ...

    def resolve(self, lg_tid: str) -> None:
        """Mark an entry resolved (no-op for unknown ids — resume paths call
        this unconditionally, including for workflows never registered)."""
        ...

    def claim(self, lg_tid: str, *, actor: str | None = None) -> bool | None:
        """Atomically claim a pending/ignored card. None means unmanaged."""
        ...

    def pending(self) -> list[PendingApproval]:
        """All entries still pending."""
        ...

    def get_entry(self, lg_tid: str) -> "PendingApproval | None":
        """The entry for ``lg_tid`` in ANY status (unlike :meth:`pending`,
        which only ever returns PENDING ones) — build prompt 31, task 3:
        this is what lets a resume path tell "unknown thread" apart from
        "this specific thread's card expired" before ever touching the
        graph. ``None`` for an unregistered id."""
        ...

    def entries(
        self, *, statuses: "tuple[str, ...] | None" = None
    ) -> "list[PendingApproval]":
        """Every entry, optionally filtered to ``statuses`` — build prompt
        31's :func:`sweep_expired` needs BOTH pending and already-ignored
        entries (an ignored entry that ages past the expiry TTL must still
        be caught), which :meth:`pending` alone cannot provide since it
        only ever returns PENDING ones."""
        ...

    def mark_expired(self, lg_tid: str) -> None:
        """The sweep's honest label once the TTL has passed (build prompt
        31, task 3) — distinct from :meth:`mark_ignored`. Callers pair this
        with actually cancelling the underlying workflow; this method only
        updates the registry's own record."""
        ...


class JsonPendingApprovals:
    """File-backed registry: ``{lg_tid: {source_ref, domain, posted_at, status}}``.

    ``posted_at`` is stored as a UTC ISO-8601 string and parsed back on read —
    round-tripped through :func:`sweep_ignored`'s age math, which is what
    actually consumes it (see the ``ingestion/state.py`` precedent for why
    the consuming path, not the field, defines the format).

    Security finding F2: the ``threading.RLock`` below only serializes
    threads inside one process — it does nothing against two overlapping
    *runtime processes* both reading, mutating, and rewriting this file, e.g.
    both claiming the same approval card. Every load-mutate-save critical
    section (``register``, ``resolve``, ``claim``, ``mark_ignored``, and the
    ``_load`` inside ``pending``) additionally holds
    ``fslock.locked(path + ".lock")``, an OS-level advisory lock that also
    serializes across processes. The in-process lock stays in place too —
    cheap, and it keeps this class safe on platforms where the file lock
    degrades to a no-op (see ``fslock.py``).
    """

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.RLock()

    def get_pending_for_source(self, source_ref: str) -> PendingApproval | None:
        for entry in self.pending():
            if entry.source_ref == source_ref:
                return entry
        return None

    def register(
        self,
        *,
        lg_tid: str,
        source_ref: str,
        domain: str,
        posted_at: datetime,
        sender: str | None = None,
        subject: str | None = None,
        priority: str | None = None,
        action: str | None = None,
    ) -> None:
        with self._lock, locked(self._path + ".lock"):
            data = self._load()
            data[lg_tid] = {
                "source_ref": source_ref,
                "domain": domain,
                "posted_at": posted_at.astimezone(timezone.utc).isoformat(),
                "status": STATUS_PENDING,
                "sender": sender,
                "subject": subject,
                "priority": priority,
                "action": action,
            }
            self._save(data)

    def resolve(self, lg_tid: str) -> None:
        with self._lock, locked(self._path + ".lock"):
            data = self._load()
            if lg_tid in data:
                data[lg_tid]["status"] = STATUS_RESOLVED
                self._save(data)

    def claim(
        self,
        lg_tid: str,
        *,
        actor: str | None = None,
        now: datetime | None = None,
    ) -> bool | None:
        """Cross-process atomic claim shared by Slack and Chat callbacks:
        the load-check-mutate-save sequence runs under both the in-process
        ``threading.RLock`` and the advisory ``fslock`` on ``path + ".lock"``
        (finding F2), so two overlapping runtime processes racing the same
        ``lg_tid`` can't both see ``STATUS_PENDING`` and both win."""
        with self._lock, locked(self._path + ".lock"):
            data = self._load()
            entry = data.get(lg_tid)
            if entry is None:
                return None
            if entry.get("status") not in (STATUS_PENDING, STATUS_IGNORED):
                return False
            entry["status"] = STATUS_RESOLVED
            entry["resolved_by"] = actor
            entry["resolved_at"] = (now or datetime.now(timezone.utc)).isoformat()
            self._save(data)
            return True

    def mark_ignored(self, lg_tid: str) -> None:
        """The sweep's honest label: unanswered past the ignore threshold,
        not human-resolved (prompt 21) — the workflow itself stays
        resumable, and a late click is protected by the apply-time
        freshness check, not by this flag. Distinct from
        :meth:`mark_expired` (build prompt 31, task 3), which DOES kill the
        workflow — see the module docstring for why these mean different
        things."""
        with self._lock, locked(self._path + ".lock"):
            data = self._load()
            if lg_tid in data:
                data[lg_tid]["status"] = STATUS_IGNORED
                self._save(data)

    def mark_expired(self, lg_tid: str) -> None:
        with self._lock, locked(self._path + ".lock"):
            data = self._load()
            if lg_tid in data:
                data[lg_tid]["status"] = STATUS_EXPIRED
                self._save(data)

    def pending(self) -> list[PendingApproval]:
        return self.entries(statuses=(STATUS_PENDING,))

    def get_entry(self, lg_tid: str) -> "PendingApproval | None":
        with self._lock, locked(self._path + ".lock"):
            data = self._load()
        raw = data.get(lg_tid)
        if raw is None:
            return None
        return self._entry_from_raw(lg_tid, raw)

    def entries(
        self, *, statuses: "tuple[str, ...] | None" = None
    ) -> "list[PendingApproval]":
        with self._lock, locked(self._path + ".lock"):
            data = self._load()
        return [
            self._entry_from_raw(tid, raw)
            for tid, raw in data.items()
            if statuses is None or raw.get("status", STATUS_PENDING) in statuses
        ]

    @staticmethod
    def _entry_from_raw(tid: str, raw: dict[str, Any]) -> PendingApproval:
        return PendingApproval(
            lg_tid=tid,
            source_ref=raw.get("source_ref", ""),
            domain=raw.get("domain", ""),
            posted_at=datetime.fromisoformat(raw["posted_at"]),
            status=raw.get("status", STATUS_PENDING),
            sender=raw.get("sender"),  # absent on pre-Phase-1 entries
            subject=raw.get("subject"),  # absent on pre-build-prompt-25 entries
            priority=raw.get("priority"),
            action=raw.get("action"),  # absent on pre-build-prompt-31 entries
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
        # Security finding F5 (Low): pending approvals sit under
        # ATTUNE_DATA_DIR — chmod explicitly rather than trust the
        # process's umask, same defense-in-depth as the other JSON state
        # stores (see importance.py/attention.py's tempfile.mkstemp, which
        # already gets 0600 for free; this file's plain-open temp file did
        # not).
        os.chmod(temp, 0o600)
        os.replace(temp, self._path)


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_approvals (
    lg_tid TEXT PRIMARY KEY,
    source_ref TEXT NOT NULL,
    domain TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    status TEXT NOT NULL,
    sender TEXT,
    subject TEXT,
    priority TEXT,
    action TEXT,
    resolved_by TEXT,
    resolved_at TEXT
)
"""


class SqlitePendingApprovals:
    """Build prompt 33, task 4: the same :class:`PendingApprovals` Protocol
    as :class:`JsonPendingApprovals`, backed by SQLite instead of a whole-
    file read/write under an advisory lock — a 25-thread notification batch
    touches this store once per thread (``get_pending_for_source`` +
    ``register``), which was ~50 whole-file JSON round trips.

    :meth:`claim` is the one method that needs real cross-process atomicity
    (two overlapping runtime processes racing the same ``lg_tid``, finding
    F2) — done here with ``BEGIN IMMEDIATE`` around the check-then-update,
    SQLite's own write-lock primitive, replacing the JSON version's
    ``fslock``-plus-``threading.RLock`` pair.
    """

    def __init__(self, path: str):
        self._path = path

    def _connect(self) -> sqlite3.Connection:
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self._path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_SQLITE_SCHEMA)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass
        return conn

    def get_pending_for_source(self, source_ref: str) -> PendingApproval | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_approvals WHERE status = ? AND source_ref = ? "
                "ORDER BY rowid LIMIT 1",
                (STATUS_PENDING, source_ref),
            ).fetchone()
        return self._entry_from_row(row) if row is not None else None

    def register(
        self,
        *,
        lg_tid: str,
        source_ref: str,
        domain: str,
        posted_at: datetime,
        sender: str | None = None,
        subject: str | None = None,
        priority: str | None = None,
        action: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO pending_approvals
                    (lg_tid, source_ref, domain, posted_at, status, sender,
                     subject, priority, action, resolved_by, resolved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    lg_tid, source_ref, domain,
                    posted_at.astimezone(timezone.utc).isoformat(),
                    STATUS_PENDING, sender, subject, priority, action,
                ),
            )

    def resolve(self, lg_tid: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE pending_approvals SET status = ? WHERE lg_tid = ?",
                (STATUS_RESOLVED, lg_tid),
            )

    def claim(
        self,
        lg_tid: str,
        *,
        actor: str | None = None,
        now: datetime | None = None,
    ) -> bool | None:
        now = now or datetime.now(timezone.utc)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM pending_approvals WHERE lg_tid = ?", (lg_tid,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None
            if row[0] not in (STATUS_PENDING, STATUS_IGNORED):
                conn.execute("ROLLBACK")
                return False
            conn.execute(
                "UPDATE pending_approvals SET status = ?, resolved_by = ?, "
                "resolved_at = ? WHERE lg_tid = ?",
                (STATUS_RESOLVED, actor, now.isoformat(), lg_tid),
            )
            conn.execute("COMMIT")
            return True
        finally:
            conn.close()

    def mark_ignored(self, lg_tid: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE pending_approvals SET status = ? WHERE lg_tid = ?",
                (STATUS_IGNORED, lg_tid),
            )

    def mark_expired(self, lg_tid: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE pending_approvals SET status = ? WHERE lg_tid = ?",
                (STATUS_EXPIRED, lg_tid),
            )

    def pending(self) -> list[PendingApproval]:
        return self.entries(statuses=(STATUS_PENDING,))

    def get_entry(self, lg_tid: str) -> "PendingApproval | None":
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_approvals WHERE lg_tid = ?", (lg_tid,),
            ).fetchone()
        return self._entry_from_row(row) if row is not None else None

    def entries(
        self, *, statuses: "tuple[str, ...] | None" = None
    ) -> "list[PendingApproval]":
        query = "SELECT * FROM pending_approvals"
        params: tuple[Any, ...] = ()
        if statuses is not None:
            placeholders = ", ".join("?" for _ in statuses)
            query += f" WHERE status IN ({placeholders})"
            params = tuple(statuses)
        query += " ORDER BY rowid"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._entry_from_row(row) for row in rows]

    @staticmethod
    def _entry_from_row(row: Any) -> PendingApproval:
        return PendingApproval(
            lg_tid=row["lg_tid"],
            source_ref=row["source_ref"],
            domain=row["domain"],
            posted_at=datetime.fromisoformat(row["posted_at"]),
            status=row["status"],
            sender=row["sender"],
            subject=row["subject"],
            priority=row["priority"],
            action=row["action"],
        )


def open_pending_approvals(settings: Any) -> "PendingApprovals":
    """The one entry point every caller (``runtime.build_runtime``,
    ``app.build_app``) uses to reach the pending-approvals registry (build
    prompt 33, task 4) — JSON or SQLite, chosen by
    ``settings.local_store_backend``, so switching backends is
    configuration, never a code change at a call site. Migrating an
    existing deployment: point ``local_store_backend`` at ``sqlite`` and
    the store starts empty (registered cards are transient, ~7 days at
    most — see :data:`DEFAULT_EXPIRY` — so no one-time data migration
    script is needed; any card pending at cutover simply gets re-created on
    its next notification)."""
    from ..config import LocalStoreBackend

    if settings.local_store_backend == LocalStoreBackend.SQLITE:
        return SqlitePendingApprovals(settings.pending_db_path)
    return JsonPendingApprovals(settings.pending_state_path)


def sweep_ignored(
    registry: PendingApprovals,
    store: MemoryStore,
    *,
    user_id: str,
    max_age: timedelta = timedelta(hours=48),
    now: datetime | None = None,
    audit_log: Any = None,
    importance_profile: Any = None,
) -> int:
    """Turn stale pending cards into IGNORED signals (design 2.2).

    Entries pending longer than ``max_age`` are marked resolved and captured
    via ``capture_action_signal(…, IGNORED)`` — exactly once per entry, since
    resolving removes them from the next sweep. Returns how many were swept.

    ``importance_profile`` (Phase 1, G5/G6) is passed straight through to
    ``capture_action_signal`` alongside each entry's ``sender``, so an
    ignored card demotes that sender the same way an explicit reject would.
    Entries registered before ``sender`` existed (or calendar holds, which
    carry no organizer) simply have ``sender=None`` — ``capture_action_signal``
    already treats that as "skip the profile write, keep the memory write".

    The captured summary (build prompt 25, task 1) names the counterpart and
    subject when the entry has them, rather than the raw ``source_ref`` (a
    Gmail thread id) the pre-fix code wrote — the content-free string that
    made ``triage._past_reactions``'s sender-scoped query unable to ever
    retrieve an ignored-card signal.

    Memory-write only: the underlying mail is untouched, and the paused
    workflow itself stays resumable in the checkpointer (a very late click
    still works — it just resumes a workflow whose ignore signal was already
    recorded, which is honest: the user *did* ignore it for two days).
    """
    now = now or datetime.now(timezone.utc)
    swept = 0
    for entry in registry.pending():
        if now - entry.posted_at < max_age:
            continue
        mark = getattr(registry, "mark_ignored", registry.resolve)
        mark(entry.lg_tid)
        age_days = (now - entry.posted_at).days
        subject_bit = f' "{entry.subject}"' if entry.subject else ""
        counterpart_bit = f" from {entry.sender}" if entry.sender else f" for {entry.source_ref}"
        capture_action_signal(
            store,
            user_id=user_id,
            domain=entry.domain,
            signal=ActionSignal.IGNORED,
            summary=(
                f"approval card{subject_bit}{counterpart_bit} left untouched "
                f"{age_days}d"
            ),
            metadata={"source_ref": entry.source_ref, "lg_tid": entry.lg_tid},
            importance_profile=importance_profile,
            sender=entry.sender,
            subject=entry.subject,
            priority=entry.priority,
        )
        if audit_log is not None:
            audit_log.record(
                thread_id=entry.lg_tid,
                workflow="draft_approve",
                events=[{
                    "event": "approval_ignored",
                    "ts": now.isoformat(),
                    "source_ref": entry.source_ref,
                    "pending_hours": round(
                        (now - entry.posted_at).total_seconds() / 3600, 1
                    ),
                }],
                domain=entry.domain,
                user_id=user_id,
            )
        swept += 1
    return swept


def sweep_expired(
    registry: PendingApprovals,
    *,
    max_age: timedelta = DEFAULT_EXPIRY,
    now: datetime | None = None,
    audit_log: Any = None,
    user_id: str | None = None,
    cancel_workflow: "Callable[[str], None] | None" = None,
) -> int:
    """Cancel workflows whose cards have sat unanswered past
    :data:`DEFAULT_EXPIRY` (build prompt 31, task 3) — the harder stop
    :func:`sweep_ignored` deliberately doesn't provide.

    Examines entries in EITHER ``STATUS_PENDING`` or ``STATUS_IGNORED``
    (via ``registry.entries``, not ``registry.pending``) — an already-
    ignored entry (48h) must still be caught once it separately crosses the
    much longer 7-day expiry line; ``registry.pending()`` alone would never
    see it again, since ``sweep_ignored`` already moved it out of that
    status.

    Two effects per expired entry, both required:

    1. ``registry.mark_expired`` — the registry's own record, distinct
       from IGNORED (see the module docstring for why they carry different
       meaning to ``grants.track_records``).
    2. ``cancel_workflow(lg_tid)`` — actually releases the underlying
       LangGraph checkpoint (production: ``checkpointer.delete_thread``,
       see ``runtime.py``'s wiring). Without this, "expired" would be a
       label with no teeth — the exact bug this task exists to fix ("a
       'cancelled' flag that leaves a resumable checkpoint"). A
       ``cancel_workflow`` failure is logged and does NOT stop the sweep —
       the registry's own EXPIRED status plus ``resume_workflow``'s
       registry-first check (see ``draft_approve.resume_workflow``) already
       refuses a resume even if the checkpoint delete itself failed,
       defense in depth.

    An ``approval_expired`` audit event is recorded per entry — distinct
    from ``sweep_ignored``'s ``approval_ignored`` — so ``grants.
    track_records``/the ledger can tell the two apart (an expiry is a fact
    about elapsed time, never a judgment about the proposal, and must never
    be folded into a rejection-shaped learning signal).
    """
    now = now or datetime.now(timezone.utc)
    swept = 0
    candidates = (
        registry.entries(statuses=(STATUS_PENDING, STATUS_IGNORED))
        if hasattr(registry, "entries")
        else []
    )
    for entry in candidates:
        if now - entry.posted_at < max_age:
            continue
        mark = getattr(registry, "mark_expired", None)
        if mark is None:
            continue
        mark(entry.lg_tid)
        if cancel_workflow is not None:
            try:
                cancel_workflow(entry.lg_tid)
            except Exception:  # noqa: BLE001 — the registry's own EXPIRED
                # status plus resume_workflow's registry-first check still
                # refuse a resume even if the checkpoint delete itself
                # failed; a sweep must never abort on one bad entry.
                logger.warning(
                    "sweep_expired: cancel_workflow failed for %s",
                    entry.lg_tid, exc_info=True,
                )
        if audit_log is not None:
            audit_log.record(
                thread_id=entry.lg_tid,
                workflow="draft_approve",
                events=[{
                    "event": "approval_expired",
                    "ts": now.isoformat(),
                    "source_ref": entry.source_ref,
                    "pending_days": (now - entry.posted_at).days,
                }],
                domain=entry.domain,
                user_id=user_id,
            )
        swept += 1
    return swept
