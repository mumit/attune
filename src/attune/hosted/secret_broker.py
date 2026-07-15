"""Fail-closed core for connector credential installation and revocation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from uuid import UUID

from .vault import LeasedCredentialIntent, PostgresSecretBrokerRepository
from .vault_crypto import EnvelopeCipher


class SecretAudit(Protocol):
    def record(
        self,
        intent: LeasedCredentialIntent,
        *,
        action: str,
        outcome: str,
    ) -> bool: ...


@dataclass(frozen=True)
class SecretBrokerResult:
    status_code: int


class SecretBroker:
    def __init__(
        self,
        *,
        vault: PostgresSecretBrokerRepository,
        cipher: EnvelopeCipher,
        audit: SecretAudit,
    ):
        self._vault = vault
        self._cipher = cipher
        self._audit = audit

    def install(
        self, intent_id: UUID, credential: Mapping[str, Any]
    ) -> SecretBrokerResult:
        intent = self._lease(intent_id, "control_plane", "install")
        if intent is None:
            return SecretBrokerResult(404)
        if not self._record(intent, action="credential.install", outcome="allowed"):
            return SecretBrokerResult(503)
        version = (intent.credential_version or 0) + 1
        try:
            encrypted = self._cipher.encrypt(
                credential,
                tenant_id=intent.tenant.tenant_id,
                connector_id=intent.connector_id,
                provider=intent.provider,
                credential_version=version,
            )
            stored = self._vault.store(intent.id, encrypted)
        except Exception:
            return SecretBrokerResult(503)
        if stored is None or stored[1] != version:
            return SecretBrokerResult(503)
        return SecretBrokerResult(
            204
            if self._record(intent, action="credential.install", outcome="observed")
            else 503
        )

    def revoke(self, intent_id: UUID) -> SecretBrokerResult:
        intent = self._lease(intent_id, "control_plane", "revoke")
        if intent is None:
            return SecretBrokerResult(404)
        if not self._record(intent, action="credential.revoke", outcome="allowed"):
            return SecretBrokerResult(503)
        try:
            revoked = self._vault.revoke(intent.id)
        except Exception:
            return SecretBrokerResult(503)
        if not revoked:
            return SecretBrokerResult(503)
        return SecretBrokerResult(
            204
            if self._record(intent, action="credential.revoke", outcome="observed")
            else 503
        )

    def _lease(
        self, intent_id: UUID, producer_kind: str, operation: str
    ) -> LeasedCredentialIntent | None:
        try:
            intent = self._vault.lease(intent_id, producer_kind=producer_kind)
        except Exception:
            return None
        if intent is None or intent.operation != operation:
            return None
        return intent

    def _record(
        self,
        intent: LeasedCredentialIntent,
        *,
        action: str,
        outcome: str,
    ) -> bool:
        try:
            return self._audit.record(intent, action=action, outcome=outcome)
        except Exception:
            return False
