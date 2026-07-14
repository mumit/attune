"""Pending-approval tracking (design 2.2's IGNORED signal + card hygiene).

Two jobs, both about what happens *around* an approval card rather than in it:

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

Deliberately about signals and card hygiene only: an expired entry's workflow
stays paused in the checkpointer and can still be resumed late — nothing here
kills or times out workflows, and nothing here writes to the underlying mail
(rule 3: IGNORED capture is a memory write, not an action).

``PendingApprovals`` is a Protocol with a JSON-file-backed implementation,
same shape as ``ingestion/state.py``: read fully, rewrite fully, fine at
single-mailbox scale.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from ..memory.base import MemoryStore
from ..memory.signals import ActionSignal, capture_action_signal

STATUS_PENDING = "pending"
STATUS_RESOLVED = "resolved"   # a human answered the card
STATUS_IGNORED = "ignored"     # the sweep expired it (still resumable)


@dataclass
class PendingApproval:
    lg_tid: str          # the LangGraph workflow thread id
    source_ref: str      # what the card is about (e.g. the Gmail thread id)
    domain: str
    posted_at: datetime  # UTC
    status: str = STATUS_PENDING


class PendingApprovals(Protocol):
    def get_pending_for_source(self, source_ref: str) -> PendingApproval | None:
        """The pending entry for a source item, or None."""
        ...

    def register(
        self, *, lg_tid: str, source_ref: str, domain: str, posted_at: datetime
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


class JsonPendingApprovals:
    """File-backed registry: ``{lg_tid: {source_ref, domain, posted_at, status}}``.

    ``posted_at`` is stored as a UTC ISO-8601 string and parsed back on read —
    round-tripped through :func:`sweep_ignored`'s age math, which is what
    actually consumes it (see the ``ingestion/state.py`` precedent for why
    the consuming path, not the field, defines the format).
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
        self, *, lg_tid: str, source_ref: str, domain: str, posted_at: datetime
    ) -> None:
        with self._lock:
            data = self._load()
            data[lg_tid] = {
                "source_ref": source_ref,
                "domain": domain,
                "posted_at": posted_at.astimezone(timezone.utc).isoformat(),
                "status": STATUS_PENDING,
            }
            self._save(data)

    def resolve(self, lg_tid: str) -> None:
        with self._lock:
            data = self._load()
            if lg_tid in data:
                data[lg_tid]["status"] = STATUS_RESOLVED
                self._save(data)

    def claim(self, lg_tid: str, *, actor: str | None = None) -> bool | None:
        """Single-process atomic claim shared by Slack and Chat callbacks."""
        with self._lock:
            data = self._load()
            entry = data.get(lg_tid)
            if entry is None:
                return None
            if entry.get("status") not in (STATUS_PENDING, STATUS_IGNORED):
                return False
            entry["status"] = STATUS_RESOLVED
            entry["resolved_by"] = actor
            entry["resolved_at"] = datetime.now(timezone.utc).isoformat()
            self._save(data)
            return True

    def mark_ignored(self, lg_tid: str) -> None:
        """The sweep's honest label: expired unanswered, not human-resolved
        (prompt 21) — the workflow itself stays resumable, and a late click
        is protected by the apply-time freshness check, not by this flag."""
        with self._lock:
            data = self._load()
            if lg_tid in data:
                data[lg_tid]["status"] = STATUS_IGNORED
                self._save(data)

    def pending(self) -> list[PendingApproval]:
        return [
            PendingApproval(
                lg_tid=tid,
                source_ref=raw.get("source_ref", ""),
                domain=raw.get("domain", ""),
                posted_at=datetime.fromisoformat(raw["posted_at"]),
                status=raw.get("status", STATUS_PENDING),
            )
            for tid, raw in self._load().items()
            if raw.get("status") == STATUS_PENDING
        ]

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
        os.replace(temp, self._path)


def sweep_ignored(
    registry: PendingApprovals,
    store: MemoryStore,
    *,
    user_id: str,
    max_age: timedelta = timedelta(hours=48),
    now: datetime | None = None,
    audit_log: Any = None,
) -> int:
    """Turn stale pending cards into IGNORED signals (design 2.2).

    Entries pending longer than ``max_age`` are marked resolved and captured
    via ``capture_action_signal(…, IGNORED)`` — exactly once per entry, since
    resolving removes them from the next sweep. Returns how many were swept.

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
        capture_action_signal(
            store,
            user_id=user_id,
            domain=entry.domain,
            signal=ActionSignal.IGNORED,
            summary=(
                f"approval card for {entry.source_ref} left untouched "
                f"{(now - entry.posted_at).days}d"
            ),
            metadata={"source_ref": entry.source_ref, "lg_tid": entry.lg_tid},
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
