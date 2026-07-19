"""Tenant-bound intent-only audit adapter for hosted workers."""

from __future__ import annotations

import hashlib

from .audit import PostgresAuditProducerRepository
from .audit_client import AuditWriterClient
from .tenant import TenantContext


class WorkerAudit:
    def __init__(
        self,
        producer: PostgresAuditProducerRepository,
        writer: AuditWriterClient,
    ):
        self._producer = producer
        self._writer = writer

    def record(
        self,
        context: TenantContext,
        *,
        action: str,
        outcome: str,
        job_id: str,
        caller_subject: str,
    ) -> None:
        audit_intent = self._producer.request(
            context,
            idempotency_key=_digest(
                f"attune-worker-audit-v1:{job_id}:{action}:{outcome}"
            ),
            actor_type="workload",
            actor_ref_hash=_digest(
                f"attune-task-delivery-subject-v1:{caller_subject}"
            ),
            action=action,
            outcome=outcome,
            target_type="job",
            target_ref_hash=_digest(f"attune-job-v1:{job_id}"),
        )
        if not self._writer.write(audit_intent.id):
            raise RuntimeError("worker audit is unavailable")


class WorkerMemoryAudit:
    """Content-free per-operation audit for hosted memory commands.

    Distinct from :class:`WorkerAudit`'s one generic per-job event: this
    records one event per memory operation (teach/inspect/forget/retrieve)
    with only a bounded count in its metadata -- never memory text, a query,
    or a memory id (docs/hosted-memory.md "Content-free audit").
    """

    def __init__(
        self,
        producer: PostgresAuditProducerRepository,
        writer: AuditWriterClient,
    ):
        self._producer = producer
        self._writer = writer

    def record(
        self,
        context: TenantContext,
        *,
        action: str,
        outcome: str,
        job_id: str,
        count: int,
    ) -> None:
        if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= 1_000_000:
            raise ValueError("count must be a bounded non-negative integer")
        audit_intent = self._producer.request(
            context,
            idempotency_key=_digest(
                f"attune-memory-audit-v1:{job_id}:{action}:{outcome}:{count}"
            ),
            actor_type="workload",
            action=action,
            outcome=outcome,
            target_type="memory",
            metadata={"count": count},
        )
        if not self._writer.write(audit_intent.id):
            raise RuntimeError("memory audit is unavailable")


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()
