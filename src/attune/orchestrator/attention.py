"""The attention store — the seam Phase 2 stage 2's unified "what matters
now" brief (``docs/future-state.md`` Phase 2, step 3) will read from.

``dispatcher.handle_source_message`` records one :class:`AttentionItem` per
ROUTINE/URGENT Slack/Chat source message here; NOISE is dropped before it
ever reaches this module (see the dispatcher docstring). This is
deliberately a read/record store only — nothing here assembles a brief,
ranks anything, or correlates across sources; that is explicitly out of
scope for stage 1 (Phase 2 steps 2-3), and this module's job is to give that
future work one durable, bounded, inspectable place to read from rather than
recomputing from the audit log.

Persistence follows the same pattern as ``orchestrator/importance.py`` and
``orchestrator/pending.py``: atomic temp-file-plus-``os.replace`` writes, a
``threading.RLock`` plus ``fslock.locked`` around every read-modify-write
critical section (security finding F2 — this file is state a scheduled poll
tick and any future CLI/brief reader can both touch).

Bounded by construction, on every write:

- **Retention window** (:data:`RETENTION_DAYS`, 7): items older than this are
  dropped before the file is rewritten. The attention store is a rolling
  window of recent signal, not a permanent record — durable history already
  lives in the audit log (``dispatcher._triage_audit_fields``'s content-free
  event per message).
- **Item cap** (:data:`MAX_ITEMS`, 200): even a very chatty set of source
  channels can't grow this file unboundedly; the oldest items are dropped
  first once the cap is exceeded.

Both bounds apply together and are documented, not tunable via environment —
this is operational state, not product configuration.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from ..fslock import locked
from .triage import Priority

MAX_ITEMS = 200
RETENTION_DAYS = 7


@dataclass(frozen=True)
class AttentionItem:
    """One recorded ROUTINE/URGENT signal from an attended source.

    ``summary`` is a bounded text excerpt (never the full untrusted message
    body verbatim beyond what ``SourceMessage.text`` already caps at) —
    enough for a future brief line, not a transcript. ``priority`` is the
    dispatcher's effective :class:`~orchestrator.triage.Priority` (NOISE
    never reaches this store at all, so in practice this is ROUTINE or
    URGENT, but the type isn't narrowed further so a future caller doesn't
    need a second enum).
    """

    source: str
    channel_ref: str
    channel_name: str
    sender_ref: str
    sender_display: str
    summary: str
    ts: datetime
    priority: Priority
    mentions_principal: bool
    thread_ref: str | None


class AttentionStore(Protocol):
    def add(self, item: AttentionItem) -> None:
        """Record one item, applying retention + the item cap on write."""
        ...

    def recent(
        self, *, since: datetime | None = None, limit: int | None = None
    ) -> list[AttentionItem]:
        """Newest-first items, optionally filtered to ``ts >= since`` and/or
        capped to ``limit``."""
        ...


def _to_dict(item: AttentionItem) -> dict[str, Any]:
    return {
        "source": item.source,
        "channel_ref": item.channel_ref,
        "channel_name": item.channel_name,
        "sender_ref": item.sender_ref,
        "sender_display": item.sender_display,
        "summary": item.summary,
        "ts": item.ts.astimezone(timezone.utc).isoformat(),
        "priority": item.priority.value,
        "mentions_principal": item.mentions_principal,
        "thread_ref": item.thread_ref,
    }


def _from_dict(raw: dict[str, Any]) -> AttentionItem:
    return AttentionItem(
        source=raw["source"],
        channel_ref=raw["channel_ref"],
        channel_name=raw["channel_name"],
        sender_ref=raw["sender_ref"],
        sender_display=raw["sender_display"],
        summary=raw["summary"],
        ts=datetime.fromisoformat(raw["ts"]),
        priority=Priority(raw["priority"]),
        mentions_principal=raw["mentions_principal"],
        thread_ref=raw.get("thread_ref"),
    )


class JsonAttentionStore:
    """File-backed store: a JSON array of recorded items, newest last.

    A plain list rather than the ``{key: {...}}`` shape used by the other
    JSON stores in this package — items here have no natural unique key
    (a channel can post many messages with the same sender/ts precision
    across providers), so this is a bounded log, not a keyed registry.
    """

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.RLock()

    def add(self, item: AttentionItem) -> None:
        with self._lock, locked(self._path + ".lock"):
            items = self._load()
            items.append(_to_dict(item))
            items = self._bounded(items)
            self._save(items)

    def recent(
        self, *, since: datetime | None = None, limit: int | None = None
    ) -> list[AttentionItem]:
        with self._lock, locked(self._path + ".lock"):
            raw_items = self._load()
        items = [_from_dict(raw) for raw in raw_items]
        items.sort(key=lambda it: it.ts, reverse=True)
        if since is not None:
            items = [it for it in items if it.ts >= since]
        if limit is not None:
            items = items[:limit]
        return items

    def _bounded(self, raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Apply the retention window then the item cap (module docstring),
        oldest-first so the cap keeps the MOST RECENT ``MAX_ITEMS``."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
        kept = [
            raw for raw in raw_items
            if _from_dict(raw).ts >= cutoff
        ]
        kept.sort(key=lambda raw: raw["ts"])
        return kept[-MAX_ITEMS:]

    def _load(self) -> list[dict[str, Any]]:
        if not os.path.exists(self._path):
            return []
        with open(self._path) as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []

    def _save(self, items: list[dict[str, Any]]) -> None:
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        directory = parent or "."
        fd, temp_path = tempfile.mkstemp(prefix=".attention-", dir=directory)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(items, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp_path, self._path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
