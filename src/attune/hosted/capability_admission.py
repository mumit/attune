"""Worker-side admission-to-dispatch producer for hosted write capabilities.

Bridges the dormant :mod:`capability_gateway` admission core to the real
dispatch spine (docs/capability-gateway.md "The next safe integration
point"). Two, deliberately separate, steps:

``record`` -- runs immediately once :class:`TypedCapabilityGateway` admits a
model-triggered proposal. It persists one immutable
``attune.capability_admissions`` row and one pending, actor-bound
``attune.approvals`` row in the *same* tenant transaction
(:class:`PostgresCapabilityAdmissionRepository`), so an admission never
exists without something a human can act on. It creates no job and no
dispatch intent -- admission is never execution authority.

``decide`` -- runs only when the bound approver later approves or rejects.
It claims the approval through the one-use, SECURITY DEFINER
``attune.claim_capability_approval`` function (migration 0043,
:meth:`PostgresApprovalRepository.claim`) and, only on a fresh or replayed
"consumed" (approved) outcome, creates the job and dispatch intent through
the existing, unmodified
:class:`~attune.hosted.dispatch.PostgresDispatchProducerRepository` and
sends the resulting intent to the private dispatch broker -- exactly the
same producer-to-broker shape already used by
``WebConversationService.send()``.

:class:`CapabilityAdmissionProducer` is deliberately a thin orchestrator
over three injected collaborators (admissions repository, approvals
repository, dispatch producer) plus the broker client, so its ``decide``
control flow -- the interesting, security-relevant part -- is fully
offline-testable with fakes (CLAUDE.md "inject ... persistence paths").
"""

from __future__ import annotations

import hashlib
import secrets
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol
from uuid import UUID

from .capability_gateway import AuthorizedCapability
from .dispatch import EnqueuedDispatch
from .repositories import ClaimedApproval, ConnectionFactory, _canonical_json
from .tenant import TenantContext, tenant_transaction

APPROVAL_LIFETIME = timedelta(minutes=15)
DISPATCH_INTENT_LIFETIME = timedelta(minutes=10)


@dataclass(frozen=True)
class RecordedAdmission:
    admission_id: UUID
    approval_id: UUID


class ApprovalRepository(Protocol):
    def claim(
        self, context: TenantContext, *, approval_id: UUID, principal_id: UUID,
        decision: str,
    ) -> ClaimedApproval | None: ...


class DispatchProducer(Protocol):
    def enqueue(
        self, context: TenantContext, *, kind: str, capability: str,
        payload: dict[str, Any], idempotency_key: bytes, expires_at: datetime,
    ) -> EnqueuedDispatch: ...


class DispatchBroker(Protocol):
    def dispatch(self, intent_id: UUID) -> bool: ...


class AdmissionRepository(Protocol):
    def record(
        self, context: TenantContext, *, authorized: AuthorizedCapability,
        destination_hash: bytes, now: datetime,
    ) -> RecordedAdmission: ...


class PostgresCapabilityAdmissionRepository:
    """Persist one gateway admission and its pending approval atomically.

    Never creates a job or dispatch intent -- admission is never execution
    authority (docs/capability-gateway.md).
    """

    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def record(
        self,
        context: TenantContext,
        *,
        authorized: AuthorizedCapability,
        destination_hash: bytes,
        now: datetime,
    ) -> RecordedAdmission:
        if not isinstance(authorized, AuthorizedCapability):
            raise TypeError("a gateway-authorized capability is required")
        if not isinstance(destination_hash, bytes) or len(destination_hash) != 32:
            raise ValueError("destination_hash must be exactly 32 bytes")
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        payload = dict(authorized.arguments)
        action_hash = hashlib.sha256(
            _canonical_json(
                {
                    "capability": authorized.capability,
                    "version": authorized.contract_version,
                    "arguments": payload,
                }
            ).encode()
        ).digest()
        opaque_ref_hash = hashlib.sha256(secrets.token_bytes(32)).digest()
        expires_at = now + APPROVAL_LIFETIME

        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    INSERT INTO attune.capability_admissions
                        (tenant_id, principal_id, connector_id, capability,
                         contract_version, risk, policy_version, arguments)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    RETURNING id
                    """,
                    (
                        context.tenant_id,
                        authorized.principal_id,
                        authorized.connector_id,
                        authorized.capability,
                        authorized.contract_version,
                        int(authorized.risk),
                        authorized.policy_version,
                        _canonical_json(payload),
                    ),
                )
                admission_id = cursor.fetchone()[0]
                cursor.execute(
                    """
                    INSERT INTO attune.approvals
                        (tenant_id, admission_id, approver_id, connector_id,
                         opaque_ref_hash, action_hash, capability,
                         destination_hash, source_version, policy_version,
                         surface, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'web', %s)
                    RETURNING id
                    """,
                    (
                        context.tenant_id,
                        admission_id,
                        authorized.principal_id,
                        authorized.connector_id,
                        opaque_ref_hash,
                        action_hash,
                        authorized.capability,
                        destination_hash,
                        # Live provider resource-version freshness (e.g. the
                        # Gmail thread's historyId) is a documented
                        # remaining gate, not implemented here -- see
                        # docs/capability-gateway.md.
                        "unversioned",
                        authorized.policy_version,
                        expires_at,
                    ),
                )
                approval_id = cursor.fetchone()[0]
        return RecordedAdmission(admission_id=admission_id, approval_id=approval_id)


class CapabilityAdmissionProducer:
    def __init__(
        self,
        admissions: AdmissionRepository,
        approvals: ApprovalRepository,
        dispatch: DispatchProducer,
        broker: DispatchBroker,
        *,
        now: Callable[[], datetime] | None = None,
    ):
        self._admissions = admissions
        self._approvals = approvals
        self._dispatch = dispatch
        self._broker = broker
        self._now = now or (lambda: datetime.now(timezone.utc))

    def record(
        self,
        context: TenantContext,
        *,
        authorized: AuthorizedCapability,
        destination_hash: bytes,
    ) -> RecordedAdmission:
        now = self._now()
        if now.tzinfo is None:
            raise RuntimeError("worker clock must be timezone-aware")
        return self._admissions.record(
            context,
            authorized=authorized,
            destination_hash=destination_hash,
            now=now,
        )

    def decide(
        self,
        context: TenantContext,
        *,
        approval_id: UUID,
        principal_id: UUID,
        decision: str,
    ) -> str:
        """Claim the approval and, only when it is (freshly or on replay)
        consumed, create the job + dispatch intent through the existing
        dispatch producer and send it to the private broker.

        Returns the claim's final status: ``rejected``, ``expired``,
        ``consumed``, or ``not_found`` when no matching approval exists for
        this approver in the caller's tenant.
        """

        claimed = self._approvals.claim(
            context,
            approval_id=approval_id,
            principal_id=principal_id,
            decision=decision,
        )
        if claimed is None:
            return "not_found"
        if claimed.final_status != "consumed":
            return claimed.final_status
        if (
            claimed.capability is None
            or claimed.arguments is None
            or claimed.connector_id is None
        ):
            # Claimed via the job_id-bound approval shape (a different,
            # not-yet-built ceremony) -- nothing for this producer to
            # dispatch.
            return claimed.final_status

        now = self._now()
        if now.tzinfo is None:
            raise RuntimeError("worker clock must be timezone-aware")
        idempotency_key = hashlib.sha256(
            f"attune-capability-dispatch-v1:{context.tenant_id}:{claimed.approval_id}".encode()
        ).digest()
        payload: dict[str, Any] = {
            "schema_version": 1,
            "admission_id": str(claimed.admission_id),
            "connector_id": str(claimed.connector_id),
            **_string_keyed(claimed.arguments),
        }
        enqueued = self._dispatch.enqueue(
            context,
            kind=claimed.capability,
            capability=claimed.capability,
            payload=payload,
            idempotency_key=idempotency_key,
            expires_at=now + DISPATCH_INTENT_LIFETIME,
        )
        if not self._broker.dispatch(enqueued.intent.id):
            raise RuntimeError("capability dispatch was refused")
        return claimed.final_status


def _string_keyed(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError("admitted arguments must be a string-keyed mapping")
    return dict(value)
