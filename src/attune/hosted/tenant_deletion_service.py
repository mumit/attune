"""Audited orchestration for the owner-initiated tenant deletion ceremony.

Mirrors ``hosted_policy_service.py``: durable pre-effect audit is mandatory
before the database mutation, and the observed/failed outcome is recorded
after -- this is the authority-changing half of the ceremony (create the
durable request, or cancel it during grace). The executor half (claiming and
walking the registry once grace has elapsed) lives in
``tenant_deletion_executor.py`` and audits every relation itself through the
database functions in migration 0046.
"""

from __future__ import annotations

import hashlib
from typing import Protocol
from uuid import UUID

from .audit import PostgresAuditProducerRepository
from .audit_client import AuditWriterClient
from .tenant import TenantContext
from .tenant_deletion import (
    PostgresTenantDeletionRequests,
    TenantDeletionCancellation,
    TenantDeletionRequest,
)


class AuditWriter(Protocol):
    def write(self, audit_intent_id: UUID) -> bool: ...


class TenantDeletionService:
    def __init__(
        self,
        requests: PostgresTenantDeletionRequests,
        audit: PostgresAuditProducerRepository,
        writer: AuditWriterClient | AuditWriter,
    ):
        self._requests = requests
        self._audit = audit
        self._writer = writer

    def request(
        self, context: TenantContext, *, principal_id: UUID, session_id: UUID
    ) -> TenantDeletionRequest:
        if not self._record(
            context,
            principal_id=principal_id,
            session_id=session_id,
            action="hosted.deletion.requested",
            outcome="allowed",
        ):
            raise RuntimeError("tenant deletion pre-effect audit is unavailable")
        try:
            result = self._requests.request(
                context, principal_id=principal_id, session_id=session_id
            )
        except Exception:
            try:
                self._record(
                    context,
                    principal_id=principal_id,
                    session_id=session_id,
                    action="hosted.deletion.requested",
                    outcome="failed",
                )
            except Exception:
                pass
            raise
        if not self._record(
            context,
            principal_id=principal_id,
            session_id=session_id,
            action="hosted.deletion.requested",
            outcome="observed",
        ):
            raise RuntimeError("tenant deletion outcome audit is unavailable")
        return result

    def cancel(
        self, context: TenantContext, *, principal_id: UUID, session_id: UUID
    ) -> TenantDeletionCancellation:
        if not self._record(
            context,
            principal_id=principal_id,
            session_id=session_id,
            action="hosted.deletion.cancelled",
            outcome="allowed",
        ):
            raise RuntimeError("tenant deletion cancel pre-effect audit is unavailable")
        try:
            result = self._requests.cancel(
                context, principal_id=principal_id, session_id=session_id
            )
        except Exception:
            try:
                self._record(
                    context,
                    principal_id=principal_id,
                    session_id=session_id,
                    action="hosted.deletion.cancelled",
                    outcome="failed",
                )
            except Exception:
                pass
            raise
        if not self._record(
            context,
            principal_id=principal_id,
            session_id=session_id,
            action="hosted.deletion.cancelled",
            outcome="observed" if result.cancelled else "failed",
        ):
            raise RuntimeError("tenant deletion cancel outcome audit is unavailable")
        return result

    def status(self, context: TenantContext, *, principal_id: UUID):
        return self._requests.read(context, principal_id=principal_id)

    def _record(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        session_id: UUID,
        action: str,
        outcome: str,
    ) -> bool:
        intent = self._audit.request(
            context,
            idempotency_key=hashlib.sha256(
                b"attune-hosted-deletion-v1:"
                + action.encode("ascii")
                + b":"
                + session_id.bytes
                + b":"
                + outcome.encode("ascii")
            ).digest(),
            actor_type="principal",
            actor_ref_hash=hashlib.sha256(principal_id.bytes).digest(),
            action=action,
            outcome=outcome,
            target_type="tenant_account",
            metadata={"schema_version": 1},
        )
        return self._writer.write(intent.id)
