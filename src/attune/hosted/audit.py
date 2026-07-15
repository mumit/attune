"""Tenant-bound audit outbox and private writer repositories."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from .repositories import (
    ConnectionFactory,
    _bounded_object,
    _bounded_text,
    _canonical_json,
    _fixed_hash,
)
from .tenant import TenantContext, tenant_transaction

AUDIT_PRODUCER_KINDS = frozenset({"control_plane", "worker", "secret_broker"})
AUDIT_OUTCOMES = frozenset({"allowed", "denied", "failed", "observed"})


@dataclass(frozen=True)
class HostedAuditIntent:
    id: UUID
    producer_kind: str
    action: str
    outcome: str
    state: str
    audit_event_id: UUID | None


class PostgresAuditProducerRepository:
    """Persist an idempotent audit intent inside a producer's RLS context."""

    def __init__(
        self,
        connection_factory: ConnectionFactory,
        *,
        producer_kind: str,
    ):
        if producer_kind not in AUDIT_PRODUCER_KINDS:
            raise ValueError("this producer has no direct audit-intent role")
        self._connect = connection_factory
        self._producer_kind = producer_kind

    def request(
        self,
        context: TenantContext,
        *,
        idempotency_key: bytes,
        actor_type: str,
        action: str,
        outcome: str,
        actor_ref_hash: bytes | None = None,
        target_type: str | None = None,
        target_ref_hash: bytes | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> HostedAuditIntent:
        _fixed_hash("idempotency_key", idempotency_key)
        _bounded_text("actor_type", actor_type, 64)
        _bounded_text("action", action, 120)
        if outcome not in AUDIT_OUTCOMES:
            raise ValueError("invalid audit outcome")
        if actor_ref_hash is not None:
            _fixed_hash("actor_ref_hash", actor_ref_hash)
        if target_type is not None:
            _bounded_text("target_type", target_type, 64)
        if target_ref_hash is not None:
            _fixed_hash("target_ref_hash", target_ref_hash)
        fields = {} if metadata is None else metadata
        _bounded_object("metadata", fields, 16_384)

        values = (
            context.tenant_id,
            self._producer_kind,
            idempotency_key,
            actor_type,
            actor_ref_hash,
            action,
            outcome,
            target_type,
            target_ref_hash,
            _canonical_json(fields),
        )
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    INSERT INTO attune.audit_intents (
                        tenant_id, producer_kind, idempotency_key, actor_type,
                        actor_ref_hash, action, outcome, target_type,
                        target_ref_hash, metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                    RETURNING id, producer_kind, action, outcome, state,
                              audit_event_id
                    """,
                    values,
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        """
                        SELECT id, producer_kind, action, outcome, state,
                               audit_event_id, actor_type, actor_ref_hash,
                               target_type, target_ref_hash, metadata
                          FROM attune.audit_intents
                         WHERE tenant_id = %s AND idempotency_key = %s
                        """,
                        (context.tenant_id, idempotency_key),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise RuntimeError("idempotent audit intent disappeared")
                    if (
                        row[1] != self._producer_kind
                        or row[2] != action
                        or row[3] != outcome
                        or row[6] != actor_type
                        or row[7] != actor_ref_hash
                        or row[8] != target_type
                        or row[9] != target_ref_hash
                        or row[10] != fields
                    ):
                        raise RuntimeError(
                            "idempotency key reused for a different audit intent"
                        )
                return HostedAuditIntent(*row[:6])


class PostgresDispatchAuditRepository:
    """Create a content-free audit intent from canonical dispatch state."""

    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def request(
        self,
        dispatch_intent_id: UUID,
        *,
        outcome: str,
        error_code: str | None = None,
    ) -> UUID | None:
        if outcome not in {"allowed", "observed", "failed"}:
            raise ValueError("invalid dispatch audit outcome")
        if outcome != "failed" and error_code is not None:
            raise ValueError("error_code is valid only for failed dispatch")
        if error_code is not None:
            _bounded_text("error_code", error_code, 80)
        with closing(self._connect()) as connection:
            try:
                with closing(connection.cursor()) as cursor:
                    cursor.execute(
                        "SELECT attune.request_dispatch_audit(%s, %s, %s)",
                        (dispatch_intent_id, outcome, error_code),
                    )
                    intent_id = cursor.fetchone()[0]
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return intent_id


class PostgresAuditWriterRepository:
    """Convert one opaque durable intent into one hash-chained audit event."""

    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def write(self, audit_intent_id: UUID) -> UUID | None:
        with closing(self._connect()) as connection:
            try:
                with closing(connection.cursor()) as cursor:
                    cursor.execute(
                        "SELECT attune.write_audit_intent(%s)",
                        (audit_intent_id,),
                    )
                    event_id = cursor.fetchone()[0]
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return event_id
