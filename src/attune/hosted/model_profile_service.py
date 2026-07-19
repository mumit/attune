"""Mandatory-audit orchestration for the hosted tenant model profile
preference (docs/future-state.md Phase 6 "hosted operations"; per-tenant
model configuration). Mirrors ``hosted_channel_service.py`` exactly: the
SECURITY DEFINER function that mutates ``attune.tenant_model_preferences``
(migration 0047) contains no audit_intents write of its own -- the mandatory
allowed/observed/failed two-phase audit lives here, at the Python service
layer, exactly like ``HostedChannelService``."""

from __future__ import annotations

import hashlib
from typing import Protocol
from uuid import UUID, uuid4

from .audit import PostgresAuditProducerRepository
from .audit_client import AuditWriterClient
from .model_profile import PostgresTenantModelProfileRepository, TenantModelProfile
from .tenant import TenantContext


class AuditWriter(Protocol):
    def write(self, audit_intent_id: UUID) -> bool: ...


class HostedModelProfileService:
    def __init__(
        self,
        profiles: PostgresTenantModelProfileRepository,
        audit: PostgresAuditProducerRepository,
        writer: AuditWriterClient | AuditWriter,
    ):
        self._profiles = profiles
        self._audit = audit
        self._writer = writer

    def read(self, context: TenantContext) -> TenantModelProfile | None:
        return self._profiles.read(context)

    def configure(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        session_id: UUID,
        profile: object,
    ) -> TenantModelProfile:
        if not isinstance(profile, str):
            raise ValueError("model profile is invalid")
        target = hashlib.sha256(profile.encode("ascii", errors="strict")).digest()
        attempt_id = uuid4()
        if not self._record(
            context,
            principal_id=principal_id,
            session_id=session_id,
            target=target,
            attempt_id=attempt_id,
            outcome="allowed",
        ):
            raise RuntimeError("model profile pre-effect audit is unavailable")
        try:
            result = self._profiles.set(
                context,
                principal_id=principal_id,
                session_id=session_id,
                profile=profile,
            )
        except Exception:
            try:
                self._record(
                    context,
                    principal_id=principal_id,
                    session_id=session_id,
                    target=target,
                    attempt_id=attempt_id,
                    outcome="failed",
                )
            except Exception:
                pass
            raise
        if not self._record(
            context,
            principal_id=principal_id,
            session_id=session_id,
            target=target,
            attempt_id=attempt_id,
            outcome="observed",
        ):
            raise RuntimeError("model profile outcome audit is unavailable")
        return result

    def _record(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        session_id: UUID,
        target: bytes,
        attempt_id: UUID,
        outcome: str,
    ) -> bool:
        intent = self._audit.request(
            context,
            idempotency_key=hashlib.sha256(
                b"attune-model-profile-v1:"
                + attempt_id.bytes
                + b":"
                + session_id.bytes
                + b":"
                + target
                + b":"
                + outcome.encode("ascii")
            ).digest(),
            actor_type="principal",
            actor_ref_hash=hashlib.sha256(principal_id.bytes).digest(),
            action="hosted.model_profile.configure",
            outcome=outcome,
            target_type="tenant_model_preferences",
            target_ref_hash=target,
            metadata={"schema_version": 1},
        )
        return self._writer.write(intent.id)
