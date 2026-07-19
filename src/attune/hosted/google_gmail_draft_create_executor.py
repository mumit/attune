"""Deterministic worker executor for the fixed Gmail draft-create write.

Mirrors :class:`~attune.hosted.google_gmail_profile_executor
.GoogleGmailProfileExecutor` exactly: it is registered as an ordinary
:class:`~attune.hosted.worker_dispatch.TaskRoute`, so
:class:`~attune.hosted.worker_dispatch.WorkerDispatcher` already gives it
pre/post-effect audit and reconciliation-on-ambiguity for free. It never
runs unless the job it is claiming was created by
:class:`~attune.hosted.capability_admission.CapabilityAdmissionProducer`
*after* a human approved the exact draft (docs/capability-gateway.md).
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol
from uuid import UUID

from .repositories import HostedJob
from .tenant import TenantContext
from .vault import CredentialIntent, PostgresCredentialIntentRepository

CAPABILITY = "google.gmail.draft.create"
MAX_DRAFT_BODY_CHARS = 10_000
_PAYLOAD_KEYS = frozenset(
    {"schema_version", "admission_id", "connector_id", "thread_ref", "body"}
)
_THREAD_REF = re.compile(r"^[A-Za-z0-9_-]{1,180}$")


class IntentRepository(Protocol):
    def request(
        self,
        context: TenantContext,
        *,
        connector_id: UUID,
        operation: str,
        capability: str,
        idempotency_key: bytes,
        expires_at: datetime,
    ) -> CredentialIntent: ...


class GmailDraftCreateBroker(Protocol):
    def google_gmail_draft_create(
        self, intent_id: UUID, *, thread_ref: str, body: str
    ) -> str: ...


class GoogleGmailDraftCreateExecutor:
    def __init__(
        self,
        intents: PostgresCredentialIntentRepository | IntentRepository,
        broker: GmailDraftCreateBroker,
        *,
        now: Callable[[], datetime] | None = None,
    ):
        self._intents = intents
        self._broker = broker
        self._now = now or (lambda: datetime.now(timezone.utc))

    def __call__(self, context: TenantContext, job: HostedJob) -> None:
        if not isinstance(context, TenantContext):
            raise TypeError("verified tenant context is required")
        if job.kind != CAPABILITY or job.capability != CAPABILITY:
            raise ValueError("Gmail draft-create job does not match the fixed route")
        payload = job.payload
        if (
            not isinstance(payload, dict)
            or set(payload) != _PAYLOAD_KEYS
            or payload.get("schema_version") != 1
        ):
            raise ValueError("Gmail draft-create payload does not match the contract")
        connector_id = _parse_uuid(payload["connector_id"], "connector_id")
        _parse_uuid(payload["admission_id"], "admission_id")
        thread_ref = payload["thread_ref"]
        body = payload["body"]
        if not isinstance(thread_ref, str) or not _THREAD_REF.fullmatch(thread_ref):
            raise ValueError("thread_ref must be a bounded Gmail thread identifier")
        if not isinstance(body, str) or not 1 <= len(body) <= MAX_DRAFT_BODY_CHARS:
            raise ValueError("body must contain between 1 and 10,000 characters")

        now = self._now()
        if now.tzinfo is None:
            raise RuntimeError("worker clock must be timezone-aware")
        key = hashlib.sha256(
            (
                f"attune-google-gmail-draft-create-v1:{context.tenant_id}:"
                f"{job.id}:{connector_id}"
            ).encode()
        ).digest()
        intent = self._intents.request(
            context,
            connector_id=connector_id,
            operation="use",
            capability=CAPABILITY,
            idempotency_key=key,
            expires_at=now + timedelta(minutes=2),
        )
        if intent.state == "consumed":
            return
        if intent.state != "requested":
            raise RuntimeError("credential intent is not available")
        self._broker.google_gmail_draft_create(intent.id, thread_ref=thread_ref, body=body)


def _parse_uuid(value: object, field: str) -> UUID:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise ValueError(f"{field} must be a canonical UUID") from error
    if str(parsed) != value:
        raise ValueError(f"{field} must be a canonical UUID")
    return parsed
