"""Owner-facing producer for the hosted proactive brief job
(``docs/future-state.md`` Phase 5 item 4; G12).

Mirrors :class:`~attune.hosted.google_connection_test.GoogleWorkspaceConnectionTest`
and :class:`~attune.hosted.web_conversation.WebConversationService`: create a
canonical job + dispatch intent through the existing, unmodified
:class:`~attune.hosted.dispatch.PostgresDispatchProducerRepository`, then send
it to the private dispatch broker. No new producer machinery.

**Idempotent per tenant per hour, by construction, not by a separate check.**
The idempotency key folds in the current UTC hour
(``YYYYMMDDHH``) alongside tenant and principal, so two calls within the
same clock hour derive the identical key and
``PostgresDispatchProducerRepository.enqueue``'s own
``ON CONFLICT (tenant_id, idempotency_key) DO NOTHING`` returns the SAME
canonical job both times -- this is the documented bound: at most one
``channel.brief.deliver`` job per tenant per principal per UTC hour. A call
in the next hour derives a new key and creates a new job. Recurring
scheduling (a cron-like trigger that calls this once a day without an owner
click) is explicitly future operator work -- see ``docs/roadmap.md``,
mirroring the retention scheduler's own "separate, non-database scheduler
identity" pattern (``protocol_retention.py``) rather than inventing a second
one here.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol
from uuid import UUID

from .brief_delivery import CAPABILITY, PURPOSE
from .dispatch import EnqueuedDispatch, PostgresDispatchProducerRepository
from .tenant import TenantContext

DISPATCH_INTENT_LIFETIME = timedelta(minutes=10)


class DispatchBroker(Protocol):
    def dispatch(self, intent_id: UUID) -> bool: ...


@dataclass(frozen=True)
class StartedBrief:
    job_id: UUID


class HostedBriefProducer:
    """``POST /v1/brief/run``'s service: enqueue + dispatch one proactive
    brief job for the calling owner, idempotent per tenant per hour."""

    def __init__(
        self,
        dispatches: PostgresDispatchProducerRepository,
        broker: DispatchBroker,
        *,
        now: Callable[[], datetime] | None = None,
    ):
        self._dispatches = dispatches
        self._broker = broker
        self._now = now or (lambda: datetime.now(timezone.utc))

    def run(self, context: TenantContext, *, principal_id: UUID) -> StartedBrief:
        if not isinstance(context, TenantContext) or not isinstance(principal_id, UUID):
            raise TypeError("verified tenant context and principal UUID are required")
        now = self._now()
        if now.tzinfo is None:
            raise RuntimeError("worker clock must be timezone-aware")
        hour_bucket = now.astimezone(timezone.utc).strftime("%Y%m%d%H")
        idempotency_key = hashlib.sha256(
            f"attune-hosted-brief-v1:{context.tenant_id}:{principal_id}:{hour_bucket}".encode()
        ).digest()
        enqueued: EnqueuedDispatch = self._dispatches.enqueue(
            context,
            kind=PURPOSE,
            capability=CAPABILITY,
            payload={"schema_version": 1, "principal_id": str(principal_id)},
            idempotency_key=idempotency_key,
            expires_at=now + DISPATCH_INTENT_LIFETIME,
        )
        if not self._broker.dispatch(enqueued.intent.id):
            raise RuntimeError("brief dispatch was refused")
        return StartedBrief(job_id=enqueued.job.id)
