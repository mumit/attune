"""Mandatory-audit orchestration for hosted channel setup attempts."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import UUID, uuid4

from .audit import PostgresAuditProducerRepository
from .audit_client import AuditWriterClient
from .channel_setup import (
    MECHANISMS,
    HostedChannelSetupTransaction,
    PostgresHostedChannelSetupRepository,
)
from .tenant import TenantContext


class AuditWriter(Protocol):
    def write(self, audit_intent_id: UUID) -> bool: ...


@dataclass(frozen=True, repr=False)
class StartedChannelSetup:
    transaction: HostedChannelSetupTransaction
    one_time_secret: str

    def __repr__(self) -> str:
        return f"StartedChannelSetup(transaction={self.transaction!r}, one_time_secret=<redacted>)"


class HostedChannelSetupService:
    def __init__(
        self,
        setups: PostgresHostedChannelSetupRepository,
        audit: PostgresAuditProducerRepository,
        writer: AuditWriterClient | AuditWriter,
    ):
        self._setups = setups
        self._audit = audit
        self._writer = writer

    def read(self, context: TenantContext, *, principal_id: UUID):
        return self._setups.read(context, principal_id=principal_id)

    def begin(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        session_id: UUID,
        provider: str,
        now: datetime | None = None,
    ) -> StartedChannelSetup:
        mechanism = MECHANISMS.get(provider)
        if mechanism is None:
            raise ValueError("unsupported channel provider")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("channel setup time must be timezone-aware")
        secret = secrets.token_urlsafe(32)
        secret_hash = hashlib.sha256(secret.encode("ascii")).digest()
        attempt_id = uuid4()
        target = hashlib.sha256(
            f"attune-channel-setup-v1:{provider}:{mechanism}".encode("ascii")
        ).digest()
        if not self._record(
            context,
            principal_id=principal_id,
            session_id=session_id,
            target=target,
            attempt_id=attempt_id,
            outcome="allowed",
        ):
            raise RuntimeError("channel setup pre-effect audit is unavailable")
        try:
            transaction = self._setups.begin(
                context,
                principal_id=principal_id,
                session_id=session_id,
                provider=provider,
                mechanism=mechanism,
                secret_hash=secret_hash,
                expires_at=current + timedelta(minutes=9),
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
            raise RuntimeError("channel setup outcome audit is unavailable")
        return StartedChannelSetup(transaction, secret)

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
                b"attune-hosted-channel-setup-v1:"
                + attempt_id.bytes
                + b":"
                + session_id.bytes
                + b":"
                + outcome.encode("ascii")
            ).digest(),
            actor_type="principal",
            actor_ref_hash=hashlib.sha256(principal_id.bytes).digest(),
            action="hosted.channels.setup.begin",
            outcome=outcome,
            target_type="channel_setup",
            target_ref_hash=target,
            metadata={"schema_version": 1},
        )
        return self._writer.write(intent.id)
