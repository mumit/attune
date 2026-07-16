"""Audited orchestration for the fixed hosted policy ceremony."""

from __future__ import annotations

import hashlib
from typing import Protocol
from uuid import UUID

from .audit import PostgresAuditProducerRepository
from .audit_client import AuditWriterClient
from .hosted_policy import HostedPolicyActivation, PostgresHostedPolicyRepository
from .tenant import TenantContext


class AuditWriter(Protocol):
    def write(self, audit_intent_id: UUID) -> bool: ...


class HostedPolicyService:
    """Require durable pre-effect audit and record the observed outcome."""

    def __init__(
        self,
        policies: PostgresHostedPolicyRepository,
        audit: PostgresAuditProducerRepository,
        writer: AuditWriterClient | AuditWriter,
    ):
        self._policies = policies
        self._audit = audit
        self._writer = writer

    def activate_read_only(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        session_id: UUID,
    ) -> HostedPolicyActivation:
        if not isinstance(session_id, UUID):
            raise TypeError("session_id must be a UUID")
        if not self._record(
            context,
            principal_id=principal_id,
            session_id=session_id,
            outcome="allowed",
        ):
            raise RuntimeError("policy pre-effect audit is unavailable")
        try:
            result = self._policies.activate_read_only(
                context, principal_id=principal_id, session_id=session_id
            )
        except Exception:
            try:
                self._record(
                    context,
                    principal_id=principal_id,
                    session_id=session_id,
                    outcome="failed",
                )
            except Exception:
                pass
            raise
        outcome = "observed" if result.status == "validated" else "failed"
        if not self._record(
            context,
            principal_id=principal_id,
            session_id=session_id,
            outcome=outcome,
        ):
            raise RuntimeError("policy outcome audit is unavailable")
        return result

    def _record(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        session_id: UUID,
        outcome: str,
    ) -> bool:
        intent = self._audit.request(
            context,
            idempotency_key=hashlib.sha256(
                b"attune-hosted-policy-v1:"
                + session_id.bytes
                + b":"
                + outcome.encode("ascii")
            ).digest(),
            actor_type="principal",
            actor_ref_hash=hashlib.sha256(principal_id.bytes).digest(),
            action="hosted.policy.read_only.activate",
            outcome=outcome,
            target_type="policy_profile",
            target_ref_hash=hashlib.sha256(b"private_alpha_read_only").digest(),
            metadata={"profile_version": 1},
        )
        return self._writer.write(intent.id)
