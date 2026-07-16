"""Durable producer and function-only broker adapters for connector credentials."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from .repositories import ConnectionFactory, _bounded_text, _fixed_hash
from .tenant import TenantContext, tenant_transaction
from .vault_crypto import EncryptedCredential


@dataclass(frozen=True)
class CredentialIntent:
    id: UUID
    connector_id: UUID
    producer_kind: str
    operation: str
    capability: str
    state: str


@dataclass(frozen=True)
class LeasedCredentialIntent:
    id: UUID
    tenant: TenantContext
    connector_id: UUID
    provider: str
    operation: str
    capability: str
    credential_id: UUID | None
    credential_version: int | None
    encrypted: EncryptedCredential | None


class PostgresCredentialIntentRepository:
    def __init__(self, connection_factory: ConnectionFactory, *, producer_kind: str):
        if producer_kind not in {"control_plane", "worker"}:
            raise ValueError("invalid credential producer kind")
        self._connect = connection_factory
        self._producer_kind = producer_kind

    def request(
        self,
        context: TenantContext,
        *,
        connector_id: UUID,
        operation: str,
        capability: str,
        idempotency_key: bytes,
        expires_at: datetime,
    ) -> CredentialIntent:
        allowed = {
            "control_plane": {"install", "revoke"},
            "worker": {"use"},
        }
        if operation not in allowed[self._producer_kind]:
            raise ValueError("credential operation is not allowed for producer")
        _bounded_text("capability", capability, 120)
        _fixed_hash("idempotency_key", idempotency_key)
        if expires_at.tzinfo is None or expires_at <= datetime.now(expires_at.tzinfo):
            raise ValueError("expires_at must be a future timezone-aware value")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    INSERT INTO attune.credential_intents
                        (tenant_id, connector_id, producer_kind, operation,
                         capability, idempotency_key, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                    RETURNING id, connector_id, producer_kind, operation,
                              capability, state
                    """,
                    (
                        context.tenant_id,
                        connector_id,
                        self._producer_kind,
                        operation,
                        capability,
                        idempotency_key,
                        expires_at,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        """
                        SELECT id, connector_id, producer_kind, operation,
                               capability, state
                          FROM attune.credential_intents
                         WHERE tenant_id = %s AND idempotency_key = %s
                        """,
                        (context.tenant_id, idempotency_key),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise RuntimeError("idempotent credential intent disappeared")
                    if tuple(row[1:5]) != (
                        connector_id,
                        self._producer_kind,
                        operation,
                        capability,
                    ):
                        raise RuntimeError(
                            "idempotency key reused for a different credential intent"
                        )
                return CredentialIntent(*row)


class PostgresSecretBrokerRepository:
    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def lease(
        self, intent_id: UUID, *, producer_kind: str, lease_seconds: int = 30
    ) -> LeasedCredentialIntent | None:
        if producer_kind not in {"control_plane", "worker"}:
            raise ValueError("invalid credential producer kind")
        if not 1 <= lease_seconds <= 300:
            raise ValueError("lease_seconds must be between 1 and 300")
        row = self._call_row(
            "SELECT * FROM attune.lease_credential_intent(%s, %s, %s)",
            (intent_id, producer_kind, lease_seconds),
        )
        if row is None:
            return None
        encrypted = None
        if row[6] is not None:
            encrypted = EncryptedCredential(
                ciphertext=bytes(row[9]),
                nonce=bytes(row[10]),
                wrapped_dek=bytes(row[11]),
                key_resource=row[12],
                format_version=row[8],
            )
        return LeasedCredentialIntent(
            id=row[0],
            tenant=TenantContext(row[1]),
            connector_id=row[2],
            provider=row[3],
            operation=row[4],
            capability=row[5],
            credential_id=row[6],
            credential_version=row[7],
            encrypted=encrypted,
        )

    def store(
        self,
        intent_id: UUID,
        encrypted: EncryptedCredential,
        *,
        granted_scopes: tuple[str, ...] | None = None,
    ) -> tuple[UUID, int] | None:
        if granted_scopes is not None:
            return self._call_row(
                "SELECT * FROM attune.store_google_oauth_credential(%s,%s,%s,%s,%s,%s,%s)",
                (
                    intent_id,
                    encrypted.ciphertext,
                    encrypted.nonce,
                    encrypted.wrapped_dek,
                    encrypted.key_resource,
                    encrypted.format_version,
                    list(granted_scopes),
                ),
            )
        row = self._call_row(
            "SELECT * FROM attune.store_connector_credential(%s,%s,%s,%s,%s,%s)",
            (
                intent_id,
                encrypted.ciphertext,
                encrypted.nonce,
                encrypted.wrapped_dek,
                encrypted.key_resource,
                encrypted.format_version,
            ),
        )
        return (row[0], row[1]) if row is not None else None

    def revoke(self, intent_id: UUID) -> bool:
        row = self._call_row(
            "SELECT attune.revoke_connector_credential(%s)", (intent_id,)
        )
        return bool(row[0])

    def finalize(self, intent_id: UUID, *, producer_kind: str, outcome: str) -> bool:
        if outcome not in {"consumed", "failed"}:
            raise ValueError("invalid credential outcome")
        row = self._call_row(
            "SELECT attune.finalize_credential_intent(%s,%s,%s)",
            (intent_id, producer_kind, outcome),
        )
        return bool(row[0])

    def _call_row(self, sql: str, parameters: tuple):
        with closing(self._connect()) as connection:
            try:
                with closing(connection.cursor()) as cursor:
                    cursor.execute(sql, parameters)
                    row = cursor.fetchone()
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return row
