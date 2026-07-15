"""Tenant-bound, content-free records for ambiguous hosted job effects."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from .repositories import ConnectionFactory, HostedJob, _fixed_hash
from .tenant import TenantContext, tenant_transaction

RECONCILIATION_REASONS = frozenset(
    {
        "pre_effect_audit",
        "executor_ambiguous",
        "post_effect_audit",
        "job_finalize",
    }
)


@dataclass(frozen=True)
class HostedJobReconciliation:
    id: UUID
    job_id: UUID
    reason_code: str
    provider_request_ref_hash: bytes | None
    state: str
    result_code: str | None
    opened_at: datetime
    resolved_at: datetime | None


class PostgresJobReconciliationRepository:
    """Atomically move one leased job into explicit reconciliation."""

    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def open(
        self,
        context: TenantContext,
        job: HostedJob,
        *,
        reason_code: str,
        provider_request_ref_hash: bytes | None = None,
    ) -> HostedJobReconciliation:
        if reason_code not in RECONCILIATION_REASONS:
            raise ValueError("invalid reconciliation reason")
        if provider_request_ref_hash is not None:
            _fixed_hash("provider_request_ref_hash", provider_request_ref_hash)

        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    UPDATE attune.jobs
                       SET state = 'reconcile', lease_expires_at = NULL,
                           updated_at = clock_timestamp()
                     WHERE tenant_id = %s AND id = %s AND state = 'leased'
                       AND kind = %s AND capability = %s
                    """,
                    (context.tenant_id, job.id, job.kind, job.capability),
                )
                if cursor.rowcount != 1:
                    cursor.execute(
                        """
                        SELECT 1 FROM attune.jobs
                         WHERE tenant_id = %s AND id = %s
                           AND state = 'reconcile' AND kind = %s
                           AND capability = %s
                        """,
                        (context.tenant_id, job.id, job.kind, job.capability),
                    )
                    if cursor.fetchone() is None:
                        raise RuntimeError("job is not eligible for reconciliation")

                cursor.execute(
                    """
                    INSERT INTO attune.job_reconciliations (
                        tenant_id, job_id, reason_code,
                        provider_request_ref_hash
                    ) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (tenant_id, job_id) DO NOTHING
                    RETURNING id, job_id, reason_code,
                              provider_request_ref_hash, state, result_code,
                              opened_at, resolved_at
                    """,
                    (
                        context.tenant_id,
                        job.id,
                        reason_code,
                        provider_request_ref_hash,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        """
                        SELECT id, job_id, reason_code,
                               provider_request_ref_hash, state, result_code,
                               opened_at, resolved_at
                          FROM attune.job_reconciliations
                         WHERE tenant_id = %s AND job_id = %s
                        """,
                        (context.tenant_id, job.id),
                    )
                    row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("reconciliation record disappeared")
                return HostedJobReconciliation(*row)
