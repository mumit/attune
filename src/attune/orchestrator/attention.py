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

Hosted seam (``docs/future-state.md`` Phase 5 item 1, gap G18): callers
(``brief.py``, ``orchestrator/correlation.py``) depend only on the
:class:`AttentionStore` protocol and the :class:`AttentionItem` shape above,
never on the JSON file format. Hosted consumes this via
``attune.hosted.intelligence.PostgresAttentionStore``, which is bound to one
``TenantContext``/principal at construction (so its ``add``/``recent``
methods have the exact ``AttentionStore`` shape) and applies the same
retention-window-then-item-cap bounding as :class:`JsonAttentionStore`, as
application logic on every write — not the separate hosted
``protocol_retention`` batch job, which is a reviewed, narrower scope (short-
lived OAuth/session/provider-event protocol state); see
``attune.hosted.intelligence`` for the documented reasons.
"""

from __future__ import annotations

import json
import os
import sqlite3
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
    def add(self, item: AttentionItem, *, now: datetime | None = None) -> None:
        """Record one item, applying retention + the item cap on write.

        ``now`` anchors the retention cutoff; absent, the wall clock is used
        (production default). Callers that need hermetic tests inject it.
        """
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

    def add(self, item: AttentionItem, *, now: datetime | None = None) -> None:
        with self._lock, locked(self._path + ".lock"):
            items = self._load()
            items.append(_to_dict(item))
            items = self._bounded(items, now=now)
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

    def _bounded(
        self, raw_items: list[dict[str, Any]], *, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Apply the retention window then the item cap (module docstring),
        oldest-first so the cap keeps the MOST RECENT ``MAX_ITEMS``."""
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=RETENTION_DAYS)
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


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS attention_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    channel_ref TEXT NOT NULL,
    channel_name TEXT NOT NULL,
    sender_ref TEXT NOT NULL,
    sender_display TEXT NOT NULL,
    summary TEXT NOT NULL,
    ts TEXT NOT NULL,
    priority TEXT NOT NULL,
    mentions_principal INTEGER NOT NULL,
    thread_ref TEXT
)
"""


class SqliteAttentionStore:
    """Build prompt 33, task 4: the same :class:`AttentionStore` Protocol as
    :class:`JsonAttentionStore`, backed by SQLite — one row per item instead
    of rewriting the whole bounded array on every ``add``. Applies the exact
    same retention-window-then-item-cap bounding as :meth:`JsonAttentionStore._bounded`."""

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

    def add(self, item: AttentionItem, *, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=RETENTION_DAYS)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO attention_items
                    (source, channel_ref, channel_name, sender_ref,
                     sender_display, summary, ts, priority,
                     mentions_principal, thread_ref)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.source, item.channel_ref, item.channel_name,
                    item.sender_ref, item.sender_display, item.summary,
                    item.ts.astimezone(timezone.utc).isoformat(),
                    item.priority.value, int(item.mentions_principal),
                    item.thread_ref,
                ),
            )
            # Retention window, then the item cap (module docstring) —
            # applied in that order so the cap keeps the MOST RECENT
            # MAX_ITEMS among only the non-expired rows.
            conn.execute(
                "DELETE FROM attention_items WHERE ts < ?", (cutoff.isoformat(),),
            )
            conn.execute(
                """
                DELETE FROM attention_items WHERE id NOT IN (
                    SELECT id FROM attention_items ORDER BY ts DESC LIMIT ?
                )
                """,
                (MAX_ITEMS,),
            )

    def recent(
        self, *, since: datetime | None = None, limit: int | None = None
    ) -> list[AttentionItem]:
        query = "SELECT * FROM attention_items"
        params: list[Any] = []
        if since is not None:
            query += " WHERE ts >= ?"
            params.append(since.astimezone(timezone.utc).isoformat())
        query += " ORDER BY ts DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._item_from_row(row) for row in rows]

    @staticmethod
    def _item_from_row(row: Any) -> AttentionItem:
        return AttentionItem(
            source=row["source"],
            channel_ref=row["channel_ref"],
            channel_name=row["channel_name"],
            sender_ref=row["sender_ref"],
            sender_display=row["sender_display"],
            summary=row["summary"],
            ts=datetime.fromisoformat(row["ts"]),
            priority=Priority(row["priority"]),
            mentions_principal=bool(row["mentions_principal"]),
            thread_ref=row["thread_ref"],
        )


def open_attention_store(settings: Any) -> "AttentionStore":
    """The one entry point every caller (``runtime.build_runtime``) uses to
    reach the attention store (build prompt 33, task 4) — JSON or SQLite,
    chosen by ``settings.local_store_backend``. Migrating an existing
    deployment: the SQLite store starts empty; items already age out within
    :data:`RETENTION_DAYS` (7) regardless, so no one-time import is
    provided (see ``docs/decisions.md``)."""
    from ..config import LocalStoreBackend

    if settings.local_store_backend == LocalStoreBackend.SQLITE:
        return SqliteAttentionStore(settings.attention_db_path)
    return JsonAttentionStore(settings.attention_path)
