"""Tenant-mandatory repositories for hosted jobs, memories, and audit."""

from __future__ import annotations

import json
import math
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Sequence
from uuid import UUID

from .tenant import TenantContext, tenant_transaction

ConnectionFactory = Callable[[], Any]


@dataclass(frozen=True)
class HostedJob:
    id: UUID
    kind: str
    state: str
    capability: str
    payload: dict[str, Any]
    attempts: int
    available_at: datetime
    lease_expires_at: datetime | None


@dataclass(frozen=True)
class HostedMemory:
    id: UUID
    principal_id: UUID
    content: str
    source_class: str
    confidence: float
    score: float | None = None


@dataclass(frozen=True)
class HostedApproval:
    id: UUID
    job_id: UUID
    approver_id: UUID
    connector_id: UUID
    action_hash: bytes
    capability: str
    destination_hash: bytes
    source_version: str
    policy_version: int
    status: str
    expires_at: datetime


class PostgresJobRepository:
    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def enqueue(
        self,
        context: TenantContext,
        *,
        kind: str,
        capability: str,
        payload: dict[str, Any],
        idempotency_key: bytes,
    ) -> HostedJob:
        _bounded_text("kind", kind, 80)
        _bounded_text("capability", capability, 120)
        _fixed_hash("idempotency_key", idempotency_key)
        _bounded_object("payload", payload, 262_144)
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    INSERT INTO attune.jobs
                        (tenant_id, kind, capability, payload, idempotency_key)
                    VALUES (%s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                    RETURNING id, kind, state, capability, payload, attempts,
                              available_at, lease_expires_at
                    """,
                    (
                        context.tenant_id,
                        kind,
                        capability,
                        _canonical_json(payload),
                        idempotency_key,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        """
                        SELECT id, kind, state, capability, payload, attempts,
                               available_at, lease_expires_at
                          FROM attune.jobs
                         WHERE tenant_id = %s AND idempotency_key = %s
                        """,
                        (context.tenant_id, idempotency_key),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise RuntimeError("idempotent job disappeared")
                    if row[1] != kind or row[3] != capability or row[4] != payload:
                        raise RuntimeError("idempotency key reused for a different job")
                return _job(row)

    def get(self, context: TenantContext, job_id: UUID) -> HostedJob | None:
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    SELECT id, kind, state, capability, payload, attempts,
                           available_at, lease_expires_at
                      FROM attune.jobs WHERE tenant_id = %s AND id = %s
                    """,
                    (context.tenant_id, job_id),
                )
                row = cursor.fetchone()
                return _job(row) if row is not None else None

    def claim(
        self,
        context: TenantContext,
        job_id: UUID,
        *,
        expected_kind: str,
        expected_capability: str,
        lease_seconds: int = 300,
    ) -> HostedJob | None:
        _bounded_text("expected_kind", expected_kind, 80)
        _bounded_text("expected_capability", expected_capability, 120)
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    UPDATE attune.jobs
                       SET state = 'leased', attempts = attempts + 1,
                           lease_expires_at = clock_timestamp()
                               + (%s * interval '1 second'),
                           updated_at = clock_timestamp()
                     WHERE tenant_id = %s AND id = %s AND state = 'queued'
                       AND available_at <= clock_timestamp()
                       AND kind = %s AND capability = %s
                    RETURNING id, kind, state, capability, payload, attempts,
                              available_at, lease_expires_at
                    """,
                    (
                        lease_seconds,
                        context.tenant_id,
                        job_id,
                        expected_kind,
                        expected_capability,
                    ),
                )
                row = cursor.fetchone()
                return _job(row) if row is not None else None

    def finish(
        self,
        context: TenantContext,
        job_id: UUID,
        *,
        outcome: str,
    ) -> bool:
        if outcome not in {"succeeded", "failed", "reconcile", "cancelled"}:
            raise ValueError("invalid terminal job outcome")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    UPDATE attune.jobs
                       SET state = %s, lease_expires_at = NULL,
                           updated_at = clock_timestamp()
                     WHERE tenant_id = %s AND id = %s AND state = 'leased'
                    """,
                    (outcome, context.tenant_id, job_id),
                )
                return cursor.rowcount == 1

    def schedule_retry(
        self,
        context: TenantContext,
        job_id: UUID,
        *,
        expected_attempt: int,
        error_code: str,
        available_at: datetime,
    ) -> HostedJob | None:
        """Atomically record one retry and release the matching lease."""

        if expected_attempt < 1:
            raise ValueError("expected_attempt must be positive")
        _bounded_text("error_code", error_code, 80)
        if available_at.tzinfo is None:
            raise ValueError("available_at must be timezone-aware")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    INSERT INTO attune.job_retries
                        (tenant_id, job_id, attempt, error_code, available_at)
                    SELECT tenant_id, id, attempts, %s, %s
                      FROM attune.jobs
                     WHERE tenant_id = %s AND id = %s AND state = 'leased'
                       AND attempts = %s AND lease_expires_at IS NOT NULL
                    ON CONFLICT (tenant_id, job_id, attempt) DO NOTHING
                    RETURNING attempt
                    """,
                    (
                        error_code,
                        available_at,
                        context.tenant_id,
                        job_id,
                        expected_attempt,
                    ),
                )
                if cursor.fetchone() is None:
                    return None
                cursor.execute(
                    """
                    UPDATE attune.jobs
                       SET state = 'queued', available_at = %s,
                           lease_expires_at = NULL,
                           updated_at = clock_timestamp()
                     WHERE tenant_id = %s AND id = %s AND state = 'leased'
                       AND attempts = %s
                    RETURNING id, kind, state, capability, payload, attempts,
                              available_at, lease_expires_at
                    """,
                    (
                        available_at,
                        context.tenant_id,
                        job_id,
                        expected_attempt,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("leased job changed during retry scheduling")
                return _job(row)


class PostgresMemoryRepository:
    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def add(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        creator_id: UUID | None,
        content: str,
        provenance: dict[str, Any],
        source_class: str,
        confidence: float,
        model: str,
        embedding: Sequence[float],
    ) -> HostedMemory:
        _bounded_text("content", content, 65_536)
        _bounded_text("model", model, 255)
        _bounded_object("provenance", provenance, 32_768)
        if source_class not in {
            "user_taught",
            "provider",
            "assistant_derived",
            "system",
        }:
            raise ValueError("invalid memory source_class")
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("confidence must be finite and between 0 and 1")
        vector = _vector_literal(embedding)
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    INSERT INTO attune.memories
                        (tenant_id, principal_id, creator_id, content,
                         provenance, source_class, confidence)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
                    RETURNING id
                    """,
                    (
                        context.tenant_id,
                        principal_id,
                        creator_id,
                        content,
                        _canonical_json(provenance),
                        source_class,
                        confidence,
                    ),
                )
                memory_id = cursor.fetchone()[0]
                cursor.execute(
                    """
                    INSERT INTO attune.memory_embeddings
                        (tenant_id, memory_id, model, dimensions, embedding)
                    VALUES (%s, %s, %s, %s, %s::attune_ext.vector)
                    """,
                    (
                        context.tenant_id,
                        memory_id,
                        model,
                        len(embedding),
                        vector,
                    ),
                )
                return HostedMemory(
                    id=memory_id,
                    principal_id=principal_id,
                    content=content,
                    source_class=source_class,
                    confidence=confidence,
                )

    def search(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        model: str,
        embedding: Sequence[float],
        limit: int = 8,
    ) -> list[HostedMemory]:
        _bounded_text("model", model, 255)
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        vector = _vector_literal(embedding)
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    SELECT memory.id, memory.principal_id, memory.content,
                           memory.source_class, memory.confidence,
                           embedding.embedding
                               OPERATOR(attune_ext.<=>) %s::attune_ext.vector AS score
                      FROM attune.memories AS memory
                      JOIN attune.memory_embeddings AS embedding
                        ON embedding.tenant_id = memory.tenant_id
                       AND embedding.memory_id = memory.id
                     WHERE memory.tenant_id = %s AND memory.principal_id = %s
                       AND memory.deleted_at IS NULL
                       AND embedding.deleted_at IS NULL
                       AND embedding.model = %s
                       AND embedding.dimensions = %s
                     ORDER BY score, memory.id
                     LIMIT %s
                    """,
                    (
                        vector,
                        context.tenant_id,
                        principal_id,
                        model,
                        len(embedding),
                        limit,
                    ),
                )
                return [
                    HostedMemory(
                        id=row[0],
                        principal_id=row[1],
                        content=row[2],
                        source_class=row[3],
                        confidence=row[4],
                        score=row[5],
                    )
                    for row in cursor.fetchall()
                ]

    def get(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        memory_id: UUID,
    ) -> HostedMemory | None:
        """One memory by id, tenant/principal-scoped (SEC-201: the
        predicate below always comes from the verified caller context, never
        from a selector the user or model typed)."""
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    SELECT id, principal_id, content, source_class, confidence
                      FROM attune.memories
                     WHERE tenant_id = %s AND principal_id = %s AND id = %s
                       AND deleted_at IS NULL
                    """,
                    (context.tenant_id, principal_id, memory_id),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return HostedMemory(
                    id=row[0],
                    principal_id=row[1],
                    content=row[2],
                    source_class=row[3],
                    confidence=row[4],
                )

    def list_recent(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        limit: int = 20,
    ) -> list[HostedMemory]:
        """Most-recent-first listing for inspection (SEC-201: the tenant and
        principal predicates below are supplied by the caller's verified
        ``TenantContext``/``principal_id``, never derived from message text
        or model output)."""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    SELECT id, principal_id, content, source_class, confidence
                      FROM attune.memories
                     WHERE tenant_id = %s AND principal_id = %s
                       AND deleted_at IS NULL
                     ORDER BY created_at DESC, id DESC
                     LIMIT %s
                    """,
                    (context.tenant_id, principal_id, limit),
                )
                return [
                    HostedMemory(
                        id=row[0],
                        principal_id=row[1],
                        content=row[2],
                        source_class=row[3],
                        confidence=row[4],
                    )
                    for row in cursor.fetchall()
                ]

    def soft_delete(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        memory_id: UUID,
    ) -> bool:
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    UPDATE attune.memories
                       SET deleted_at = clock_timestamp(),
                           updated_at = clock_timestamp()
                     WHERE tenant_id = %s AND principal_id = %s AND id = %s
                       AND deleted_at IS NULL
                    """,
                    (context.tenant_id, principal_id, memory_id),
                )
                changed = cursor.rowcount == 1
                if changed:
                    cursor.execute(
                        """
                        UPDATE attune.memory_embeddings
                           SET deleted_at = clock_timestamp()
                         WHERE tenant_id = %s AND memory_id = %s
                           AND deleted_at IS NULL
                        """,
                        (context.tenant_id, memory_id),
                    )
                return changed


class PostgresApprovalRepository:
    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def propose(
        self,
        context: TenantContext,
        *,
        job_id: UUID,
        approver_id: UUID,
        connector_id: UUID,
        opaque_ref_hash: bytes,
        action_hash: bytes,
        capability: str,
        destination_hash: bytes,
        source_version: str,
        policy_version: int,
        expires_at: datetime,
    ) -> HostedApproval:
        _fixed_hash("opaque_ref_hash", opaque_ref_hash)
        _fixed_hash("action_hash", action_hash)
        _fixed_hash("destination_hash", destination_hash)
        _bounded_text("capability", capability, 120)
        _bounded_text("source_version", source_version, 255)
        if policy_version < 1:
            raise ValueError("policy_version must be positive")
        if expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    INSERT INTO attune.approvals
                        (tenant_id, job_id, approver_id, connector_id,
                         opaque_ref_hash, action_hash, capability,
                         destination_hash, source_version, policy_version,
                         expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, job_id, approver_id, connector_id,
                              action_hash, capability, destination_hash,
                              source_version, policy_version, status, expires_at
                    """,
                    (
                        context.tenant_id,
                        job_id,
                        approver_id,
                        connector_id,
                        opaque_ref_hash,
                        action_hash,
                        capability,
                        destination_hash,
                        source_version,
                        policy_version,
                        expires_at,
                    ),
                )
                return _approval(cursor.fetchone())

    def decide(
        self,
        context: TenantContext,
        *,
        opaque_ref_hash: bytes,
        approver_id: UUID,
        decision: str,
    ) -> HostedApproval | None:
        _fixed_hash("opaque_ref_hash", opaque_ref_hash)
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    UPDATE attune.approvals
                       SET status = %s, decided_at = clock_timestamp()
                     WHERE tenant_id = %s AND opaque_ref_hash = %s
                       AND approver_id = %s AND status = 'pending'
                       AND expires_at > clock_timestamp()
                    RETURNING id, job_id, approver_id, connector_id,
                              action_hash, capability, destination_hash,
                              source_version, policy_version, status, expires_at
                    """,
                    (
                        decision,
                        context.tenant_id,
                        opaque_ref_hash,
                        approver_id,
                    ),
                )
                row = cursor.fetchone()
                return _approval(row) if row is not None else None

    def consume(
        self,
        context: TenantContext,
        *,
        approval_id: UUID,
        expected_action_hash: bytes,
        expected_source_version: str,
        expected_policy_version: int,
    ) -> HostedApproval | None:
        _fixed_hash("expected_action_hash", expected_action_hash)
        _bounded_text("expected_source_version", expected_source_version, 255)
        if expected_policy_version < 1:
            raise ValueError("expected_policy_version must be positive")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    UPDATE attune.approvals
                       SET status = 'consumed', consumed_at = clock_timestamp()
                     WHERE tenant_id = %s AND id = %s AND status = 'approved'
                       AND expires_at > clock_timestamp()
                       AND action_hash = %s AND source_version = %s
                       AND policy_version = %s
                    RETURNING id, job_id, approver_id, connector_id,
                              action_hash, capability, destination_hash,
                              source_version, policy_version, status, expires_at
                    """,
                    (
                        context.tenant_id,
                        approval_id,
                        expected_action_hash,
                        expected_source_version,
                        expected_policy_version,
                    ),
                )
                row = cursor.fetchone()
                return _approval(row) if row is not None else None


def _job(row: Sequence[Any]) -> HostedJob:
    return HostedJob(
        id=row[0],
        kind=row[1],
        state=row[2],
        capability=row[3],
        payload=row[4],
        attempts=row[5],
        available_at=row[6],
        lease_expires_at=row[7],
    )


def _approval(row: Sequence[Any]) -> HostedApproval:
    return HostedApproval(
        id=row[0],
        job_id=row[1],
        approver_id=row[2],
        connector_id=row[3],
        action_hash=bytes(row[4]),
        capability=row[5],
        destination_hash=bytes(row[6]),
        source_version=row[7],
        policy_version=row[8],
        status=row[9],
        expires_at=row[10],
    )


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _bounded_object(name: str, value: dict[str, Any], byte_limit: int) -> None:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    if len(_canonical_json(value).encode()) > byte_limit:
        raise ValueError(f"{name} exceeds its byte limit")


def _bounded_text(name: str, value: str, limit: int) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        raise ValueError(f"{name} must contain between 1 and {limit} characters")


def _fixed_hash(name: str, value: bytes) -> None:
    if not isinstance(value, bytes) or len(value) != 32:
        raise ValueError(f"{name} must be exactly 32 bytes")


def _vector_literal(values: Sequence[float]) -> str:
    if not 1 <= len(values) <= 4096:
        raise ValueError("embedding dimensions must be between 1 and 4096")
    normalized: list[str] = []
    for value in values:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("embedding values must be finite")
        normalized.append(repr(number))
    return "[" + ",".join(normalized) + "]"
