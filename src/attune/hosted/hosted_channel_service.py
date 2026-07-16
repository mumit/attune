"""Mandatory-audit orchestration for hosted channel preferences."""

from __future__ import annotations

import hashlib
import json
from typing import Protocol
from uuid import UUID, uuid4

from .audit import PostgresAuditProducerRepository
from .audit_client import AuditWriterClient
from .hosted_channels import (
    HostedChannelPreferences,
    PostgresHostedChannelRepository,
    normalize_channels,
)
from .tenant import TenantContext


class AuditWriter(Protocol):
    def write(self, audit_intent_id: UUID) -> bool: ...


class HostedChannelService:
    def __init__(
        self,
        channels: PostgresHostedChannelRepository,
        audit: PostgresAuditProducerRepository,
        writer: AuditWriterClient | AuditWriter,
    ):
        self._channels = channels
        self._audit = audit
        self._writer = writer

    def read(self, context: TenantContext, *, principal_id: UUID):
        return self._channels.read(context, principal_id=principal_id)

    def configure(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        session_id: UUID,
        interaction_channels: object,
        brief_channels: object,
    ) -> HostedChannelPreferences:
        interaction = normalize_channels("interaction_channels", interaction_channels)
        briefs = normalize_channels("brief_channels", brief_channels)
        if not interaction and not briefs:
            raise ValueError("at least one channel purpose is required")
        target = hashlib.sha256(
            json.dumps(
                {"brief": briefs, "interaction": interaction},
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        ).digest()
        attempt_id = uuid4()
        if not self._record(
            context,
            principal_id=principal_id,
            session_id=session_id,
            target=target,
            attempt_id=attempt_id,
            outcome="allowed",
        ):
            raise RuntimeError("channel pre-effect audit is unavailable")
        try:
            result = self._channels.configure(
                context,
                principal_id=principal_id,
                session_id=session_id,
                interaction_channels=interaction,
                brief_channels=briefs,
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
            raise RuntimeError("channel outcome audit is unavailable")
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
                b"attune-hosted-channels-v1:"
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
            action="hosted.channels.configure",
            outcome=outcome,
            target_type="channel_preferences",
            target_ref_hash=target,
            metadata={"schema_version": 1},
        )
        return self._writer.write(intent.id)
