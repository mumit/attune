"""Composite, data-minimized Google Workspace connection verification."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol
from uuid import UUID

from .google_gmail_profile_executor import IntentRepository
from .repositories import HostedJob
from .secret_broker_client import GmailProfile, SecretBrokerClient
from .tenant import TenantContext
from .vault import PostgresCredentialIntentRepository

CAPABILITY = "google.workspace.connection.verify"
GMAIL_CAPABILITY = "google.gmail.profile.read"
CALENDAR_CAPABILITY = "google.calendar.primary.read"


class WorkspaceBroker(Protocol):
    def google_gmail_profile(self, intent_id: UUID) -> GmailProfile: ...

    def google_calendar_primary(self, intent_id: UUID) -> None: ...


class GoogleWorkspaceVerificationExecutor:
    """Require independently authorized Gmail and Calendar fixed reads."""

    def __init__(
        self,
        intents: PostgresCredentialIntentRepository | IntentRepository,
        broker: SecretBrokerClient | WorkspaceBroker,
        *,
        now: Callable[[], datetime] | None = None,
    ):
        self._intents = intents
        self._broker = broker
        self._now = now or (lambda: datetime.now(timezone.utc))

    def __call__(self, context: TenantContext, job: HostedJob) -> None:
        connector_id = _connector_id(context, job)
        now = self._now()
        if now.tzinfo is None:
            raise RuntimeError("worker clock must be timezone-aware")
        self._use(
            context,
            job,
            connector_id,
            GMAIL_CAPABILITY,
            now,
            self._broker.google_gmail_profile,
        )
        self._use(
            context,
            job,
            connector_id,
            CALENDAR_CAPABILITY,
            now,
            self._broker.google_calendar_primary,
        )

    def _use(
        self,
        context: TenantContext,
        job: HostedJob,
        connector_id: UUID,
        capability: str,
        now: datetime,
        operation: Callable[[UUID], object],
    ) -> None:
        key = hashlib.sha256(
            (
                f"attune-google-workspace-verification-v1:{capability}:"
                f"{context.tenant_id}:{job.id}:{connector_id}"
            ).encode()
        ).digest()
        intent = self._intents.request(
            context,
            connector_id=connector_id,
            operation="use",
            capability=capability,
            idempotency_key=key,
            expires_at=now + timedelta(minutes=2),
        )
        if intent.state == "consumed":
            return
        if intent.state != "requested":
            raise RuntimeError("credential intent is not available")
        operation(intent.id)


def _connector_id(context: TenantContext, job: HostedJob) -> UUID:
    if not isinstance(context, TenantContext):
        raise TypeError("verified tenant context is required")
    if job.kind != CAPABILITY or job.capability != CAPABILITY:
        raise ValueError("Workspace verification job does not match the fixed route")
    if not isinstance(job.payload, dict) or set(job.payload) != {"connector_id"}:
        raise ValueError("Workspace verification payload does not match the contract")
    raw = job.payload["connector_id"]
    if not isinstance(raw, str):
        raise ValueError("connector_id must be a canonical UUID")
    try:
        connector_id = UUID(raw)
    except ValueError as error:
        raise ValueError("connector_id must be a canonical UUID") from error
    if str(connector_id) != raw:
        raise ValueError("connector_id must be a canonical UUID")
    return connector_id
