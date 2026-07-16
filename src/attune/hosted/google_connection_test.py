"""Principal-bound orchestration for the fixed Google Workspace connection test."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import UUID

from .dispatch import PostgresDispatchProducerRepository
from .repositories import PostgresJobRepository
from .tenant import TenantContext

CAPABILITY = "google.gmail.profile.read"
REQUIRED_SCOPES = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
)
class ConnectorRepository(Protocol):
    def active_connector(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        required_scopes: tuple[str, ...],
    ) -> UUID | None: ...


class Broker(Protocol):
    def dispatch(self, intent_id: UUID) -> bool: ...


@dataclass(frozen=True)
class StartedConnectionTest:
    job_id: UUID
    state: str = "queued"


class GoogleWorkspaceConnectionTest:
    """Enqueue and observe one data-minimized, read-only provider probe."""

    def __init__(
        self,
        connectors: ConnectorRepository,
        dispatches: PostgresDispatchProducerRepository,
        jobs: PostgresJobRepository,
        broker: Broker,
    ):
        self._connectors = connectors
        self._dispatches = dispatches
        self._jobs = jobs
        self._broker = broker

    def start(
        self, context: TenantContext, *, principal_id: UUID
    ) -> StartedConnectionTest | None:
        connector_id = self._active_connector(context, principal_id)
        if connector_id is None:
            return None
        nonce = secrets.token_bytes(32)
        key = hashlib.sha256(
            b"attune-google-connection-test-v1:"
            + context.tenant_id.bytes
            + principal_id.bytes
            + nonce
        ).digest()
        dispatch = self._dispatches.enqueue(
            context,
            kind=CAPABILITY,
            capability=CAPABILITY,
            payload={"connector_id": str(connector_id)},
            idempotency_key=key,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        if not self._broker.dispatch(dispatch.intent.id):
            raise RuntimeError("connection test dispatch was refused")
        return StartedConnectionTest(dispatch.job.id)

    def status(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        job_id: UUID,
    ) -> str | None:
        connector_id = self._active_connector(context, principal_id)
        if connector_id is None:
            return None
        job = self._jobs.get(context, job_id)
        if (
            job is None
            or job.kind != CAPABILITY
            or job.capability != CAPABILITY
            or job.payload != {"connector_id": str(connector_id)}
        ):
            return None
        if job.state in {"queued", "leased"}:
            return "queued" if job.state == "queued" else "running"
        if job.state == "succeeded":
            return "succeeded"
        if job.state in {"failed", "reconcile", "cancelled"}:
            return "failed"
        raise RuntimeError("connection test has an unknown state")

    def _active_connector(
        self, context: TenantContext, principal_id: UUID
    ) -> UUID | None:
        if not isinstance(context, TenantContext) or not isinstance(principal_id, UUID):
            raise TypeError("verified tenant and principal authority are required")
        return self._connectors.active_connector(
            context,
            principal_id=principal_id,
            required_scopes=REQUIRED_SCOPES,
        )
