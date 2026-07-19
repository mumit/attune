"""Tenant-scoped, Postgres-backed implementations of the two local Phase 1/2
intelligence stores (``docs/future-state.md`` Phase 5 item 1; gaps G8/G18):
:class:`~attune.orchestrator.importance.ImportanceProfile` and
:class:`~attune.orchestrator.attention.AttentionStore`.

This module is the hosted half of the seam documented in each of those
modules' own docstrings. Nothing here reimplements product logic: the tier
thresholds live in ``orchestrator.importance.assess_from_signals`` (imported,
not copied), and the bounding constants (``MAX_SIGNALS``, ``DECAY_DAYS``,
``MAX_ITEMS``, ``RETENTION_DAYS``) are the exact same module-level constants
the local JSON-backed stores use.

**Binding, not per-call context (the deliberate divergence from
``PostgresMemoryRepository``).** ``PostgresMemoryRepository``'s methods each
take a ``TenantContext`` argument because ``repositories.py``'s callers
already have one canonical per-call context (a request, a job). The local
``ImportanceProfile``/``AttentionStore`` protocols have no such parameter --
they were designed for a single-principal local instance where "which
principal" is never in question. Rather than changing those protocols (and
therefore every existing caller: ``orchestrator/triage.py``, ``brief.py``,
their tests), :class:`PostgresImportanceProfile` and
:class:`PostgresAttentionStore` take their ``TenantContext``/``principal_id``
at CONSTRUCTION time, exactly once per hosted job/request, and their methods
match the local protocol shapes exactly. A future hosted executor builds one
short-lived instance per job (mirroring how the local runtime builds one
``JsonImportanceProfile`` per process) and hands it straight to
``triage_thread``/``assemble_brief`` unchanged -- this is what "consumable by
hosted executors without duplication" (G18) means in practice: zero new code
in the intelligence modules themselves.

**Hashed references (the reviewed choice, not the default).** Every
sender/channel/thread reference is a keyed HMAC-SHA256 digest via
:class:`IntelligenceReferenceHasher`, computed in Python before any SQL runs
-- never a plain hash, and never plaintext at rest. See
``sql/0042_intelligence_persistence.sql``'s header comment for the full
reasoning (mirrors ``channel_broker.ChannelReferenceHasher``'s posture for
externally-supplied, potentially low-entropy identifiers). The one
consequence worth stating plainly: :meth:`PostgresImportanceProfile.senders`
and every ``channel_ref``/``sender_ref``/``thread_ref`` field on an
:class:`~attune.orchestrator.attention.AttentionItem` read back from
:class:`PostgresAttentionStore` are hex-encoded, NON-REVERSIBLE hashes, not
the original provider identifier -- unlike the local JSON stores, which keep
the real value. Nothing display-oriented is lost: ``sender_display`` and
``channel_name`` (what a brief line or an inspect surface actually shows a
human) stay plain, bounded text either way. If a future hosted surface needs
to show or re-derive the real sender for importance signals specifically (the
local CLI's ``attune importance show`` does), it will need its own paired
display column, added deliberately -- not by weakening this table's hashing.

**Dormant.** This stage wires no executor and no secret/key-management
surface for the HMAC key; :class:`IntelligenceReferenceHasher` takes a raw
32-byte key from its caller, exactly like
``channel_broker.decode_channel_reference_key`` prepares one, but no
production entry point constructs one yet. See ``docs/decisions.md`` for the
dated record of this and every other choice above.
"""

from __future__ import annotations

import hashlib
import hmac
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Protocol, Sequence
from uuid import UUID

from ..memory.signals import ActionSignal
from ..orchestrator.attention import MAX_ITEMS, RETENTION_DAYS, AttentionItem
from ..orchestrator.importance import (
    DECAY_DAYS,
    MAX_SIGNALS,
    ImportanceTier,
    TierAssessment,
    _normalize_sender,
    assess_from_signals,
)
from ..orchestrator.triage import Priority
from .repositories import ConnectionFactory, _bounded_text
from .tenant import TenantContext, tenant_transaction

_MAX_REFERENCE_LENGTH = 320
_HMAC_DOMAIN = b"attune-intelligence-ref-v1\0"


class IntelligenceReferenceHasher:
    """Keyed, domain-separated HMAC-SHA256 references for importance/
    attention sender, channel, and thread identifiers.

    Unlike ``channel_broker.ChannelReferenceHasher`` (one fixed regex per
    Google Chat reference kind), sources here span multiple providers and
    shapes with no single format -- a Gmail address, a Slack user/channel id,
    a Google Chat resource name -- so this hasher validates only a bound,
    non-empty string and relies on ``kind`` for domain separation between a
    sender, a channel, and a thread reference (so the same literal string
    used as two different kinds never collides).
    """

    def __init__(self, key: bytes):
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError("intelligence reference HMAC key must be exactly 32 bytes")
        self._key = key

    def hash(self, kind: str, value: str) -> bytes:
        if not isinstance(value, str) or not 1 <= len(value) <= _MAX_REFERENCE_LENGTH:
            raise ValueError("invalid intelligence reference")
        return hmac.new(
            self._key,
            _HMAC_DOMAIN + kind.encode("ascii") + b"\0" + value.encode("utf-8"),
            hashlib.sha256,
        ).digest()


class PostgresImportanceProfile:
    """Hosted ``ImportanceProfile`` — see the module docstring for the
    binding-at-construction and hashed-reference design notes."""

    def __init__(
        self,
        connection_factory: ConnectionFactory,
        context: TenantContext,
        principal_id: UUID,
        *,
        reference_hasher: IntelligenceReferenceHasher,
    ):
        self._connect = connection_factory
        self._context = context
        self._principal_id = principal_id
        self._hasher = reference_hasher

    def record_signal(
        self, sender: str, signal: ActionSignal, *, ts: datetime | None = None
    ) -> None:
        if not isinstance(signal, ActionSignal):
            raise ValueError("signal must be an ActionSignal")
        sender_hash = self._sender_hash(sender)
        stamp = (ts or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, self._context) as cursor:
                cursor.execute(
                    """
                    INSERT INTO attune.importance_signals
                        (tenant_id, principal_id, sender_ref_hash, kind, signal, recorded_at)
                    VALUES (%s, %s, %s, 'signal', %s, %s)
                    """,
                    (
                        self._context.tenant_id,
                        self._principal_id,
                        sender_hash,
                        signal.value,
                        stamp,
                    ),
                )
                # Bounded storage: keep only the most recent MAX_SIGNALS rows
                # per sender, mirroring JsonImportanceProfile's own
                # ``signals[-MAX_SIGNALS:]`` truncation on every write.
                cursor.execute(
                    """
                    DELETE FROM attune.importance_signals
                     WHERE tenant_id = %s AND principal_id = %s
                       AND sender_ref_hash = %s AND kind = 'signal'
                       AND id NOT IN (
                           SELECT id FROM attune.importance_signals
                            WHERE tenant_id = %s AND principal_id = %s
                              AND sender_ref_hash = %s AND kind = 'signal'
                            ORDER BY recorded_at DESC, id DESC
                            LIMIT %s
                       )
                    """,
                    (
                        self._context.tenant_id, self._principal_id, sender_hash,
                        self._context.tenant_id, self._principal_id, sender_hash,
                        MAX_SIGNALS,
                    ),
                )

    def assess(self, sender: str, *, now: datetime | None = None) -> TierAssessment:
        now = now or datetime.now(timezone.utc)
        sender_hash = self._sender_hash(sender)
        cutoff = now - timedelta(days=DECAY_DAYS)
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, self._context) as cursor:
                cursor.execute(
                    """
                    SELECT pinned_tier FROM attune.importance_signals
                     WHERE tenant_id = %s AND principal_id = %s
                       AND sender_ref_hash = %s AND kind = 'pin'
                    """,
                    (self._context.tenant_id, self._principal_id, sender_hash),
                )
                pin_row = cursor.fetchone()
                if pin_row is not None:
                    tier = ImportanceTier(pin_row[0])
                    return TierAssessment(
                        tier, f"pinned {tier.value} by the principal", True
                    )
                effective = self._effective_signals(cursor, sender_hash, cutoff)
        if not effective:
            return TierAssessment(ImportanceTier.NORMAL, "no recorded signals", False)
        return assess_from_signals(effective)

    def pin(self, sender: str, tier: ImportanceTier) -> None:
        if not isinstance(tier, ImportanceTier):
            raise ValueError("tier must be an ImportanceTier")
        sender_hash = self._sender_hash(sender)
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, self._context) as cursor:
                cursor.execute(
                    """
                    INSERT INTO attune.importance_signals
                        (tenant_id, principal_id, sender_ref_hash, kind,
                         pinned_tier, recorded_at)
                    VALUES (%s, %s, %s, 'pin', %s, clock_timestamp())
                    ON CONFLICT (tenant_id, principal_id, sender_ref_hash)
                        WHERE kind = 'pin'
                    DO UPDATE SET pinned_tier = EXCLUDED.pinned_tier,
                                  recorded_at = clock_timestamp()
                    """,
                    (self._context.tenant_id, self._principal_id, sender_hash, tier.value),
                )

    def unpin(self, sender: str) -> bool:
        sender_hash = self._sender_hash(sender)
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, self._context) as cursor:
                cursor.execute(
                    """
                    DELETE FROM attune.importance_signals
                     WHERE tenant_id = %s AND principal_id = %s
                       AND sender_ref_hash = %s AND kind = 'pin'
                    """,
                    (self._context.tenant_id, self._principal_id, sender_hash),
                )
                return cursor.rowcount == 1

    def senders(self) -> list[str]:
        """All sender references with any recorded state, as hex-encoded
        HMAC digests -- NOT the original address/ref (module docstring)."""
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, self._context) as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT sender_ref_hash FROM attune.importance_signals
                     WHERE tenant_id = %s AND principal_id = %s
                     ORDER BY sender_ref_hash
                    """,
                    (self._context.tenant_id, self._principal_id),
                )
                return [bytes(row[0]).hex() for row in cursor.fetchall()]

    def recent_signals(
        self, sender: str, *, now: datetime | None = None
    ) -> list[tuple[ActionSignal, datetime]]:
        """The recorded, non-decayed ``(signal, ts)`` pairs for ``sender``,
        oldest first -- parity with ``JsonImportanceProfile.recent_signals``."""
        now = now or datetime.now(timezone.utc)
        sender_hash = self._sender_hash(sender)
        cutoff = now - timedelta(days=DECAY_DAYS)
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, self._context) as cursor:
                return self._effective_signals(cursor, sender_hash, cutoff)

    def _sender_hash(self, sender: str) -> bytes:
        # ``hash`` itself rejects an empty/oversized reference (before any
        # connection is opened) -- see IntelligenceReferenceHasher.hash.
        return self._hasher.hash("sender", _normalize_sender(sender))

    def _effective_signals(
        self, cursor, sender_hash: bytes, cutoff: datetime
    ) -> list[tuple[ActionSignal, datetime]]:
        cursor.execute(
            """
            SELECT signal, recorded_at FROM attune.importance_signals
             WHERE tenant_id = %s AND principal_id = %s
               AND sender_ref_hash = %s AND kind = 'signal' AND recorded_at >= %s
             ORDER BY recorded_at ASC
            """,
            (self._context.tenant_id, self._principal_id, sender_hash, cutoff),
        )
        return [(ActionSignal(row[0]), row[1]) for row in cursor.fetchall()]


class PostgresAttentionStore:
    """Hosted ``AttentionStore`` — see the module docstring for the
    binding-at-construction and hashed-reference design notes."""

    def __init__(
        self,
        connection_factory: ConnectionFactory,
        context: TenantContext,
        principal_id: UUID,
        *,
        reference_hasher: IntelligenceReferenceHasher,
    ):
        self._connect = connection_factory
        self._context = context
        self._principal_id = principal_id
        self._hasher = reference_hasher

    def add(self, item: AttentionItem) -> None:
        if item.source not in {"slack", "google_chat"}:
            raise ValueError("attention item source must be slack or google_chat")
        if not isinstance(item.priority, Priority):
            raise ValueError("attention item priority must be a Priority")
        _bounded_text("channel_name", item.channel_name, 200)
        _bounded_text("sender_display", item.sender_display, 200)
        _bounded_text("summary", item.summary, 2000)
        channel_hash = self._hasher.hash("channel", item.channel_ref)
        sender_hash = self._hasher.hash("sender", item.sender_ref)
        thread_hash = (
            self._hasher.hash("thread", item.thread_ref)
            if item.thread_ref is not None else None
        )
        ts = item.ts.astimezone(timezone.utc)
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, self._context) as cursor:
                cursor.execute(
                    """
                    INSERT INTO attune.attention_items
                        (tenant_id, principal_id, source, channel_ref_hash,
                         channel_name, sender_ref_hash, sender_display, summary,
                         ts, priority, mentions_principal, thread_ref_hash)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        self._context.tenant_id, self._principal_id, item.source,
                        channel_hash, item.channel_name, sender_hash,
                        item.sender_display, item.summary, ts, item.priority.value,
                        item.mentions_principal, thread_hash,
                    ),
                )
                # Retention window then item cap, in that order -- exactly
                # JsonAttentionStore._bounded's own sequence and rationale.
                retention_cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
                cursor.execute(
                    """
                    DELETE FROM attune.attention_items
                     WHERE tenant_id = %s AND principal_id = %s AND ts < %s
                    """,
                    (self._context.tenant_id, self._principal_id, retention_cutoff),
                )
                cursor.execute(
                    """
                    DELETE FROM attune.attention_items
                     WHERE tenant_id = %s AND principal_id = %s
                       AND id NOT IN (
                           SELECT id FROM attune.attention_items
                            WHERE tenant_id = %s AND principal_id = %s
                            ORDER BY ts DESC, id DESC
                            LIMIT %s
                       )
                    """,
                    (
                        self._context.tenant_id, self._principal_id,
                        self._context.tenant_id, self._principal_id, MAX_ITEMS,
                    ),
                )

    def recent(
        self, *, since: datetime | None = None, limit: int | None = None
    ) -> list[AttentionItem]:
        if limit is not None and (not isinstance(limit, int) or limit < 0):
            raise ValueError("limit must be a non-negative integer")
        statement = (
            "SELECT source, channel_ref_hash, channel_name, sender_ref_hash, "
            "sender_display, summary, ts, priority, mentions_principal, "
            "thread_ref_hash FROM attune.attention_items "
            "WHERE tenant_id = %s AND principal_id = %s"
        )
        params: list[object] = [self._context.tenant_id, self._principal_id]
        if since is not None:
            statement += " AND ts >= %s"
            params.append(since)
        statement += " ORDER BY ts DESC"
        if limit is not None:
            statement += " LIMIT %s"
            params.append(limit)
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, self._context) as cursor:
                cursor.execute(statement, tuple(params))
                rows = cursor.fetchall()
        return [_attention_item_from_row(row) for row in rows]


class ImportanceSignalRecorder(Protocol):
    """The subset of signal capture the draft-and-approve capability's
    decision path uses (Phase 5 stage 4, G12 -- "signal capture closes the
    loop"). Deliberately narrower than the full :class:`ImportanceProfile`
    shape: a decision-path caller only ever records, never assesses/pins."""

    def record(
        self, context: TenantContext, *, principal_id: UUID, reference: str,
        signal: ActionSignal,
    ) -> None: ...


class PostgresImportanceSignalCapture:
    """Thin per-call wrapper around :class:`PostgresImportanceProfile` for
    callers (the hosted draft-and-approve decision path today) that have no
    other reason to hold a whole profile instance -- constructs one
    short-lived profile per call, exactly the "one instance per job/request"
    binding the module docstring documents, and calls only
    :meth:`~PostgresImportanceProfile.record_signal`.

    **Hosted profiles key on hashed provider references, not necessarily
    email addresses (module docstring's hashed-reference design).** The
    draft-and-approve flow never resolves a Gmail thread's actual sender --
    only a caller-typed ``thread_ref`` exists at either the propose or the
    decide step -- so ``reference`` here is that thread reference, hashed
    under the SAME ``"sender"`` HMAC domain :class:`PostgresImportanceProfile`
    already uses for a real sender address. This is a deliberate, documented
    consequence of the reference-hashing design: two independent lookup keys
    (a real sender address, or -- absent one -- a thread reference) can
    share one hashed keyspace without colliding in practice, and the tier
    this produces is scoped to "how has approving drafts on this thread
    gone", not to a resolved counterpart identity. A future hosted surface
    that resolves the real Gmail thread participant can record against that
    instead, without changing this class.
    """

    def __init__(
        self, connection_factory: ConnectionFactory, reference_hasher: IntelligenceReferenceHasher,
    ):
        self._connect = connection_factory
        self._hasher = reference_hasher

    def record(
        self, context: TenantContext, *, principal_id: UUID, reference: str,
        signal: ActionSignal,
    ) -> None:
        PostgresImportanceProfile(
            self._connect, context, principal_id, reference_hasher=self._hasher,
        ).record_signal(reference, signal)


def _attention_item_from_row(row: Sequence[object]) -> AttentionItem:
    thread_hash = row[9]
    return AttentionItem(
        source=row[0],
        channel_ref=bytes(row[1]).hex(),
        channel_name=row[2],
        sender_ref=bytes(row[3]).hex(),
        sender_display=row[4],
        summary=row[5],
        ts=row[6],
        priority=Priority(row[7]),
        mentions_principal=row[8],
        thread_ref=bytes(thread_hash).hex() if thread_hash is not None else None,
    )
