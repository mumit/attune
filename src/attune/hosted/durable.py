"""Tenant-mandatory repositories for the remaining hosted durable objects."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Sequence
from uuid import UUID

from .repositories import (
    ConnectionFactory,
    _bounded_object,
    _bounded_text,
    _canonical_json,
    _fixed_hash,
)
from .tenant import TenantContext, tenant_transaction


@dataclass(frozen=True)
class HostedProviderEvent:
    id: UUID
    installation_id: UUID
    provider: str
    kind: str
    signal: dict[str, Any]
    processed_at: datetime | None


@dataclass(frozen=True)
class HostedCheckpoint:
    workflow_id: UUID
    version: int
    state: dict[str, Any]
    status: str


@dataclass(frozen=True)
class HostedConversation:
    id: UUID
    installation_id: UUID
    principal_id: UUID
    surface: str


@dataclass(frozen=True)
class HostedTurn:
    conversation_id: UUID
    sequence: int
    actor_type: str
    content: str
    provenance: dict[str, Any]


@dataclass(frozen=True)
class HostedAutonomyGrant:
    id: UUID
    principal_id: UUID
    capability: str
    domain: str
    maximum_risk: int
    policy_version: int
    granted_by: UUID
    revoked_at: datetime | None


@dataclass(frozen=True)
class HostedExport:
    id: UUID
    requested_by: UUID
    scope: dict[str, Any]
    state: str
    object_ref: UUID | None
    expires_at: datetime | None


@dataclass(frozen=True)
class HostedDeletionMarker:
    id: UUID
    requested_by: UUID
    object_type: str
    object_ref_hash: bytes
    state: str
    suppress_restore_until: datetime


class PostgresProviderEventRepository:
    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def record(
        self,
        context: TenantContext,
        *,
        installation_id: UUID,
        provider: str,
        kind: str,
        deduplication_key: bytes,
        signal: dict[str, Any],
    ) -> HostedProviderEvent:
        if provider not in {"google", "slack"}:
            raise ValueError("unsupported provider")
        _bounded_text("kind", kind, 80)
        _fixed_hash("deduplication_key", deduplication_key)
        _bounded_object("signal", signal, 32_768)
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    INSERT INTO attune.provider_events
                        (tenant_id, installation_id, provider, kind,
                         deduplication_key, signal)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (tenant_id, provider, deduplication_key)
                    DO NOTHING
                    RETURNING id, installation_id, provider, kind, signal,
                              processed_at
                    """,
                    (
                        context.tenant_id,
                        installation_id,
                        provider,
                        kind,
                        deduplication_key,
                        _canonical_json(signal),
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        """
                        SELECT id, installation_id, provider, kind, signal,
                               processed_at
                          FROM attune.provider_events
                         WHERE tenant_id = %s AND provider = %s
                           AND deduplication_key = %s
                        """,
                        (context.tenant_id, provider, deduplication_key),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise RuntimeError("idempotent provider event disappeared")
                    if row[1] != installation_id or row[3] != kind or row[4] != signal:
                        raise RuntimeError(
                            "deduplication key reused for a different provider event"
                        )
                return _provider_event(row)

    def mark_processed(
        self, context: TenantContext, event_id: UUID
    ) -> HostedProviderEvent | None:
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    UPDATE attune.provider_events
                       SET processed_at = clock_timestamp()
                     WHERE tenant_id = %s AND id = %s AND processed_at IS NULL
                    RETURNING id, installation_id, provider, kind, signal,
                              processed_at
                    """,
                    (context.tenant_id, event_id),
                )
                row = cursor.fetchone()
                return _provider_event(row) if row is not None else None


class PostgresWorkflowRepository:
    _STATUSES = {"running", "waiting", "completed", "failed", "cancelled"}

    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def checkpoint(
        self,
        context: TenantContext,
        *,
        workflow_id: UUID,
        state: dict[str, Any],
        status: str,
        expected_version: int,
    ) -> HostedCheckpoint:
        _bounded_object("state", state, 1_048_576)
        if status not in self._STATUSES:
            raise ValueError("invalid workflow status")
        if expected_version < 0:
            raise ValueError("expected_version cannot be negative")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"{context.tenant_id}:{workflow_id}",),
                )
                cursor.execute(
                    """
                    SELECT COALESCE(max(version), 0)
                      FROM attune.workflow_checkpoints
                     WHERE tenant_id = %s AND workflow_id = %s
                    """,
                    (context.tenant_id, workflow_id),
                )
                current = cursor.fetchone()[0]
                if current != expected_version:
                    raise RuntimeError("workflow checkpoint version conflict")
                version = current + 1
                cursor.execute(
                    """
                    INSERT INTO attune.workflow_checkpoints
                        (tenant_id, workflow_id, version, state, status)
                    VALUES (%s, %s, %s, %s::jsonb, %s)
                    """,
                    (
                        context.tenant_id,
                        workflow_id,
                        version,
                        _canonical_json(state),
                        status,
                    ),
                )
                return HostedCheckpoint(workflow_id, version, state, status)

    def latest(
        self, context: TenantContext, workflow_id: UUID
    ) -> HostedCheckpoint | None:
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    SELECT workflow_id, version, state, status
                      FROM attune.workflow_checkpoints
                     WHERE tenant_id = %s AND workflow_id = %s
                     ORDER BY version DESC LIMIT 1
                    """,
                    (context.tenant_id, workflow_id),
                )
                row = cursor.fetchone()
                return HostedCheckpoint(*row) if row is not None else None


class PostgresConversationRepository:
    _SURFACES = {"slack", "google_chat", "web"}
    _ACTORS = {"user", "assistant", "system"}

    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def get_or_create(
        self,
        context: TenantContext,
        *,
        installation_id: UUID,
        principal_id: UUID,
        surface: str,
        external_ref_hash: bytes,
    ) -> HostedConversation:
        if surface not in self._SURFACES:
            raise ValueError("unsupported conversation surface")
        _fixed_hash("external_ref_hash", external_ref_hash)
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    INSERT INTO attune.conversations
                        (tenant_id, installation_id, principal_id, surface,
                         external_ref_hash)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, surface, external_ref_hash)
                    DO NOTHING
                    RETURNING id, installation_id, principal_id, surface
                    """,
                    (
                        context.tenant_id,
                        installation_id,
                        principal_id,
                        surface,
                        external_ref_hash,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        """
                        SELECT id, installation_id, principal_id, surface
                          FROM attune.conversations
                         WHERE tenant_id = %s AND surface = %s
                           AND external_ref_hash = %s
                        """,
                        (context.tenant_id, surface, external_ref_hash),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise RuntimeError("idempotent conversation disappeared")
                    if row[1] != installation_id or row[2] != principal_id:
                        raise RuntimeError(
                            "external reference reused for another conversation"
                        )
                return HostedConversation(*row)

    def append_turn(
        self,
        context: TenantContext,
        *,
        conversation_id: UUID,
        actor_type: str,
        content: str,
        provenance: dict[str, Any] | None = None,
    ) -> HostedTurn:
        if actor_type not in self._ACTORS:
            raise ValueError("invalid conversation actor")
        _bounded_text("content", content, 131_072)
        fields = {} if provenance is None else provenance
        _bounded_object("provenance", fields, 32_768)
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    SELECT id FROM attune.conversations
                     WHERE tenant_id = %s AND id = %s FOR UPDATE
                    """,
                    (context.tenant_id, conversation_id),
                )
                if cursor.fetchone() is None:
                    raise LookupError("conversation not found")
                cursor.execute(
                    """
                    SELECT COALESCE(max(sequence), 0) + 1
                      FROM attune.conversation_turns
                     WHERE tenant_id = %s AND conversation_id = %s
                    """,
                    (context.tenant_id, conversation_id),
                )
                sequence = cursor.fetchone()[0]
                cursor.execute(
                    """
                    INSERT INTO attune.conversation_turns
                        (tenant_id, conversation_id, sequence, actor_type,
                         content, provenance)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        context.tenant_id,
                        conversation_id,
                        sequence,
                        actor_type,
                        content,
                        _canonical_json(fields),
                    ),
                )
                cursor.execute(
                    """
                    UPDATE attune.conversations SET updated_at = clock_timestamp()
                     WHERE tenant_id = %s AND id = %s
                    """,
                    (context.tenant_id, conversation_id),
                )
                return HostedTurn(
                    conversation_id, sequence, actor_type, content, fields
                )

    def recent(
        self,
        context: TenantContext,
        conversation_id: UUID,
        *,
        limit: int = 20,
    ) -> list[HostedTurn]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    SELECT conversation_id, sequence, actor_type, content,
                           provenance
                      FROM attune.conversation_turns
                     WHERE tenant_id = %s AND conversation_id = %s
                     ORDER BY sequence DESC LIMIT %s
                    """,
                    (context.tenant_id, conversation_id, limit),
                )
                return [HostedTurn(*row) for row in reversed(cursor.fetchall())]


class PostgresAutonomyRepository:
    """Read active grants; mutations exist only as fixed reviewed ceremonies."""

    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def find_active(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        capability: str,
        domain: str,
    ) -> HostedAutonomyGrant | None:
        _bounded_text("capability", capability, 120)
        _bounded_text("domain", domain, 80)
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    SELECT id, principal_id, capability, domain, maximum_risk,
                           policy_version, granted_by, revoked_at
                      FROM attune.autonomy_grants
                     WHERE tenant_id = %s AND principal_id = %s
                       AND capability = %s AND domain = %s
                       AND revoked_at IS NULL
                    """,
                    (context.tenant_id, principal_id, capability, domain),
                )
                row = cursor.fetchone()
                return HostedAutonomyGrant(*row) if row is not None else None


class PostgresLifecycleRepository:
    _EXPORT_STATES = {"running", "ready", "expired", "failed", "cancelled"}
    _DELETION_STATES = {"running", "completed", "failed"}

    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def record_usage(
        self,
        context: TenantContext,
        *,
        category: str,
        provider: str,
        units: Decimal,
        attributes: dict[str, Any] | None = None,
    ) -> UUID:
        _bounded_text("category", category, 80)
        _bounded_text("provider", provider, 80)
        if not isinstance(units, Decimal) or not units.is_finite() or units < 0:
            raise ValueError("units must be a finite non-negative Decimal")
        fields = {} if attributes is None else attributes
        _bounded_object("attributes", fields, 8192)
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    INSERT INTO attune.usage_records
                        (tenant_id, category, provider, units, attributes)
                    VALUES (%s, %s, %s, %s, %s::jsonb)
                    RETURNING id
                    """,
                    (
                        context.tenant_id,
                        category,
                        provider,
                        units,
                        _canonical_json(fields),
                    ),
                )
                return cursor.fetchone()[0]

    def request_export(
        self,
        context: TenantContext,
        *,
        requested_by: UUID,
        scope: dict[str, Any],
    ) -> HostedExport:
        _bounded_object("scope", scope, 16_384)
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    INSERT INTO attune.export_jobs
                        (tenant_id, requested_by, scope)
                    VALUES (%s, %s, %s::jsonb)
                    RETURNING id, requested_by, scope, state, object_ref,
                              expires_at
                    """,
                    (context.tenant_id, requested_by, _canonical_json(scope)),
                )
                return HostedExport(*cursor.fetchone())

    def transition_export(
        self,
        context: TenantContext,
        export_id: UUID,
        *,
        expected_state: str,
        state: str,
        object_ref: UUID | None = None,
        expires_at: datetime | None = None,
    ) -> HostedExport | None:
        if state not in self._EXPORT_STATES:
            raise ValueError("invalid export state")
        if state == "ready":
            if object_ref is None or expires_at is None or expires_at.tzinfo is None:
                raise ValueError("ready exports require object_ref and aware expiry")
        elif object_ref is not None or expires_at is not None:
            raise ValueError("only ready exports may publish an object")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    UPDATE attune.export_jobs
                       SET state = %s, object_ref = %s, expires_at = %s,
                           updated_at = clock_timestamp()
                     WHERE tenant_id = %s AND id = %s AND state = %s
                    RETURNING id, requested_by, scope, state, object_ref,
                              expires_at
                    """,
                    (
                        state,
                        object_ref,
                        expires_at,
                        context.tenant_id,
                        export_id,
                        expected_state,
                    ),
                )
                row = cursor.fetchone()
                return HostedExport(*row) if row is not None else None

    def request_deletion(
        self,
        context: TenantContext,
        *,
        requested_by: UUID,
        object_type: str,
        object_ref_hash: bytes,
        suppress_restore_until: datetime,
    ) -> HostedDeletionMarker:
        _bounded_text("object_type", object_type, 80)
        _fixed_hash("object_ref_hash", object_ref_hash)
        if suppress_restore_until.tzinfo is None:
            raise ValueError("suppress_restore_until must be timezone-aware")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    INSERT INTO attune.deletion_markers
                        (tenant_id, requested_by, object_type, object_ref_hash,
                         suppress_restore_until)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, object_type, object_ref_hash)
                    DO NOTHING
                    RETURNING id, requested_by, object_type, object_ref_hash,
                              state, suppress_restore_until
                    """,
                    (
                        context.tenant_id,
                        requested_by,
                        object_type,
                        object_ref_hash,
                        suppress_restore_until,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        """
                        SELECT id, requested_by, object_type, object_ref_hash,
                               state, suppress_restore_until
                          FROM attune.deletion_markers
                         WHERE tenant_id = %s AND object_type = %s
                           AND object_ref_hash = %s
                        """,
                        (context.tenant_id, object_type, object_ref_hash),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise RuntimeError("idempotent deletion marker disappeared")
                    if row[1] != requested_by:
                        raise RuntimeError(
                            "deletion reference reused by another principal"
                        )
                return _deletion_marker(row)

    def transition_deletion(
        self,
        context: TenantContext,
        marker_id: UUID,
        *,
        expected_state: str,
        state: str,
    ) -> HostedDeletionMarker | None:
        if state not in self._DELETION_STATES:
            raise ValueError("invalid deletion state")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    UPDATE attune.deletion_markers
                       SET state = %s,
                           completed_at = CASE WHEN %s = 'completed'
                               THEN clock_timestamp() ELSE NULL END
                     WHERE tenant_id = %s AND id = %s AND state = %s
                    RETURNING id, requested_by, object_type, object_ref_hash,
                              state, suppress_restore_until
                    """,
                    (
                        state,
                        state,
                        context.tenant_id,
                        marker_id,
                        expected_state,
                    ),
                )
                row = cursor.fetchone()
                return _deletion_marker(row) if row is not None else None


def _provider_event(row: Sequence[Any]) -> HostedProviderEvent:
    return HostedProviderEvent(*row)


def _deletion_marker(row: Sequence[Any]) -> HostedDeletionMarker:
    return HostedDeletionMarker(
        id=row[0],
        requested_by=row[1],
        object_type=row[2],
        object_ref_hash=bytes(row[3]),
        state=row[4],
        suppress_restore_until=row[5],
    )
