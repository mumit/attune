"""Content-free durable audit path for secret-broker effects."""

from __future__ import annotations

import hashlib
from typing import Protocol
from uuid import UUID

from .audit import PostgresAuditProducerRepository
from .vault import LeasedCredentialIntent


class IntentWriter(Protocol):
    def write(self, audit_intent_id: UUID) -> bool: ...


class SecretBrokerAudit:
    """Create tenant-bound audit intents and synchronously persist them."""

    def __init__(
        self,
        producer: PostgresAuditProducerRepository,
        writer: IntentWriter,
    ):
        self._producer = producer
        self._writer = writer

    def record(
        self,
        intent: LeasedCredentialIntent,
        *,
        action: str,
        outcome: str,
    ) -> bool:
        idempotency_key = _digest(
            f"attune-secret-audit-v1:{intent.id}:{action}:{outcome}"
        )
        target_ref_hash = _digest(
            f"attune-connector-v1:{intent.connector_id}"
        )
        audit_intent = self._producer.request(
            intent.tenant,
            idempotency_key=idempotency_key,
            actor_type="workload",
            action=action,
            outcome=outcome,
            target_type="connector",
            target_ref_hash=target_ref_hash,
        )
        return self._writer.write(audit_intent.id)


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()
