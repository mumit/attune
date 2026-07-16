"""Tenant-safe repositories for hosted OAuth transaction state."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence
from uuid import UUID, uuid4

from .repositories import ConnectionFactory, _fixed_hash
from .tenant import TenantContext, tenant_transaction


@dataclass(frozen=True)
class HostedOAuthTransaction:
    id: UUID
    principal_id: UUID
    connector_id: UUID
    credential_intent_id: UUID
    provider: str
    state: str
    attempts: int
    expires_at: datetime


@dataclass(frozen=True)
class StartedGoogleOAuth:
    """Canonical authority created for one browser authorization attempt."""

    connector_id: UUID
    credential_intent_id: UUID
    transaction_id: UUID


@dataclass(frozen=True, repr=False)
class LeasedOAuthTransaction:
    id: UUID
    context: TenantContext
    principal_id: UUID
    connector_id: UUID
    credential_intent_id: UUID
    provider: str
    nonce_hash: bytes
    pkce_verifier: str
    redirect_uri: str
    scopes: tuple[str, ...]

    def __repr__(self) -> str:
        return (
            "LeasedOAuthTransaction("
            f"id={self.id!r}, context={self.context!r}, "
            f"principal_id={self.principal_id!r}, connector_id={self.connector_id!r}, "
            f"provider={self.provider!r}, nonce_hash=<redacted>, "
            "pkce_verifier=<redacted>, redirect_uri=<redacted>, scopes=<redacted>)"
        )


class PostgresOAuthTransactionRepository:
    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def create(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        connector_id: UUID,
        credential_intent_id: UUID,
        state_hash: bytes,
        binding_hash: bytes,
        nonce_hash: bytes,
        pkce_verifier: str,
        redirect_uri: str,
        scopes: Sequence[str],
        expires_at: datetime,
    ) -> HostedOAuthTransaction:
        for name, value in (
            ("state_hash", state_hash),
            ("binding_hash", binding_hash),
            ("nonce_hash", nonce_hash),
        ):
            _fixed_hash(name, value)
        if not all(
            isinstance(value, UUID)
            for value in (principal_id, connector_id, credential_intent_id)
        ):
            raise TypeError(
                "principal_id, connector_id, and credential_intent_id must be UUIDs"
            )
        if not isinstance(pkce_verifier, str) or not 43 <= len(pkce_verifier) <= 128:
            raise ValueError("invalid PKCE verifier")
        if not pkce_verifier.replace("-", "A").replace("_", "A").isalnum():
            raise ValueError("invalid PKCE verifier")
        if not isinstance(redirect_uri, str) or not redirect_uri.startswith("https://"):
            raise ValueError("OAuth redirect URI must use HTTPS")
        normalized_scopes = tuple(scopes)
        if not 1 <= len(normalized_scopes) <= 32 or any(
            not isinstance(scope, str) or not 1 <= len(scope) <= 255
            for scope in normalized_scopes
        ):
            raise ValueError("invalid OAuth scopes")
        if len(set(normalized_scopes)) != len(normalized_scopes):
            raise ValueError("OAuth scopes must be unique")
        if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
            raise ValueError("OAuth expiry must be timezone-aware")

        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    INSERT INTO attune.oauth_transactions
                        (tenant_id, principal_id, connector_id,
                         credential_intent_id, provider,
                         state_hash, binding_hash, nonce_hash, pkce_verifier,
                         redirect_uri, scopes, expires_at)
                    VALUES (%s, %s, %s, %s, 'google', %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, principal_id, connector_id,
                              credential_intent_id, provider, state, attempts,
                              expires_at
                    """,
                    (
                        context.tenant_id,
                        principal_id,
                        connector_id,
                        credential_intent_id,
                        state_hash,
                        binding_hash,
                        nonce_hash,
                        pkce_verifier,
                        redirect_uri,
                        list(normalized_scopes),
                        expires_at,
                    ),
                )
                return _hosted_transaction(cursor.fetchone())


class PostgresGoogleOAuthStartRepository:
    """Atomically create a principal-bound Google install authorization.

    The browser never supplies tenant, principal, connector, intent, provider,
    redirect, or scope authority. A transaction-scoped advisory lock prevents
    concurrent requests from creating multiple pending Google connectors for
    the same principal.
    """

    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def is_connected(self, context: TenantContext, *, principal_id: UUID) -> bool:
        if not isinstance(principal_id, UUID):
            raise TypeError("principal_id must be a UUID")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM attune.connectors
                         WHERE tenant_id = %s AND principal_id = %s
                           AND provider = 'google' AND status = 'active'
                    )
                    """,
                    (context.tenant_id, principal_id),
                )
                return bool(cursor.fetchone()[0])

    def start(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        state_hash: bytes,
        binding_hash: bytes,
        nonce_hash: bytes,
        pkce_verifier: str,
        redirect_uri: str,
        scopes: Sequence[str],
        expires_at: datetime,
    ) -> StartedGoogleOAuth:
        for name, value in (
            ("state_hash", state_hash),
            ("binding_hash", binding_hash),
            ("nonce_hash", nonce_hash),
        ):
            _fixed_hash(name, value)
        if not isinstance(principal_id, UUID):
            raise TypeError("principal_id must be a UUID")
        if not isinstance(pkce_verifier, str) or not 43 <= len(pkce_verifier) <= 128:
            raise ValueError("invalid PKCE verifier")
        if not pkce_verifier.replace("-", "A").replace("_", "A").isalnum():
            raise ValueError("invalid PKCE verifier")
        if not isinstance(redirect_uri, str) or not redirect_uri.startswith("https://"):
            raise ValueError("OAuth redirect URI must use HTTPS")
        normalized_scopes = tuple(scopes)
        if not 1 <= len(normalized_scopes) <= 32 or any(
            not isinstance(scope, str) or not 1 <= len(scope) <= 255
            for scope in normalized_scopes
        ):
            raise ValueError("invalid OAuth scopes")
        if len(set(normalized_scopes)) != len(normalized_scopes):
            raise ValueError("OAuth scopes must be unique")
        if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
            raise ValueError("OAuth expiry must be timezone-aware")

        credential_ref = uuid4()
        intent_key = uuid4().bytes + uuid4().bytes
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"{context.tenant_id}:{principal_id}:google",),
                )
                cursor.execute(
                    """
                    SELECT id, status
                      FROM attune.connectors
                     WHERE tenant_id = %s AND principal_id = %s
                       AND provider = 'google' AND status IN ('pending', 'active')
                     ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,
                              created_at DESC
                     LIMIT 1
                    """,
                    (context.tenant_id, principal_id),
                )
                connector = cursor.fetchone()
                if connector is not None and connector[1] == "active":
                    raise RuntimeError("Google Workspace is already connected")
                if connector is None:
                    cursor.execute(
                        """
                        INSERT INTO attune.connectors
                            (tenant_id, principal_id, provider, credential_ref)
                        VALUES (%s, %s, 'google', %s)
                        RETURNING id
                        """,
                        (context.tenant_id, principal_id, credential_ref),
                    )
                    connector_id = cursor.fetchone()[0]
                else:
                    connector_id = connector[0]
                cursor.execute(
                    """
                    INSERT INTO attune.credential_intents
                        (tenant_id, connector_id, producer_kind, operation,
                         capability, idempotency_key, expires_at)
                    VALUES (%s, %s, 'control_plane', 'install',
                            'google.oauth.install', %s, %s)
                    RETURNING id
                    """,
                    (context.tenant_id, connector_id, intent_key, expires_at),
                )
                intent_id = cursor.fetchone()[0]
                cursor.execute(
                    """
                    INSERT INTO attune.oauth_transactions
                        (tenant_id, principal_id, connector_id,
                         credential_intent_id, provider, state_hash,
                         binding_hash, nonce_hash, pkce_verifier, redirect_uri,
                         scopes, expires_at)
                    VALUES (%s, %s, %s, %s, 'google', %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        context.tenant_id,
                        principal_id,
                        connector_id,
                        intent_id,
                        state_hash,
                        binding_hash,
                        nonce_hash,
                        pkce_verifier,
                        redirect_uri,
                        list(normalized_scopes),
                        expires_at,
                    ),
                )
                transaction_id = cursor.fetchone()[0]
                return StartedGoogleOAuth(
                    connector_id=connector_id,
                    credential_intent_id=intent_id,
                    transaction_id=transaction_id,
                )


class PostgresOAuthExchangeRepository:
    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def lease(
        self, *, state_hash: bytes, binding_hash: bytes, lease_seconds: int = 30
    ) -> LeasedOAuthTransaction | None:
        _fixed_hash("state_hash", state_hash)
        _fixed_hash("binding_hash", binding_hash)
        if not 1 <= lease_seconds <= 60:
            raise ValueError("OAuth lease_seconds must be between 1 and 60")
        with closing(self._connect()) as connection:
            try:
                with closing(connection.cursor()) as cursor:
                    cursor.execute(
                        """
                        SELECT transaction_id, tenant_id, principal_id,
                               connector_id, credential_intent_id, provider, nonce_hash,
                               pkce_verifier, redirect_uri, scopes
                          FROM attune.lease_oauth_transaction(%s, %s, %s)
                        """,
                        (state_hash, binding_hash, lease_seconds),
                    )
                    row = cursor.fetchone()
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        if row is None:
            return None
        return LeasedOAuthTransaction(
            id=row[0],
            context=TenantContext(row[1]),
            principal_id=row[2],
            connector_id=row[3],
            credential_intent_id=row[4],
            provider=row[5],
            nonce_hash=row[6],
            pkce_verifier=row[7],
            redirect_uri=row[8],
            scopes=tuple(row[9]),
        )

    def finalize(
        self, transaction_id: UUID, *, binding_hash: bytes, outcome: str
    ) -> bool:
        if not isinstance(transaction_id, UUID):
            raise TypeError("transaction_id must be a UUID")
        _fixed_hash("binding_hash", binding_hash)
        if outcome not in {"completed", "failed"}:
            raise ValueError("invalid OAuth transaction outcome")
        with closing(self._connect()) as connection:
            try:
                with closing(connection.cursor()) as cursor:
                    cursor.execute(
                        "SELECT attune.finalize_oauth_transaction(%s, %s, %s)",
                        (transaction_id, binding_hash, outcome),
                    )
                    finalized = bool(cursor.fetchone()[0])
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return finalized


def _hosted_transaction(row) -> HostedOAuthTransaction:
    return HostedOAuthTransaction(
        id=row[0],
        principal_id=row[1],
        connector_id=row[2],
        credential_intent_id=row[3],
        provider=row[4],
        state=row[5],
        attempts=row[6],
        expires_at=row[7],
    )
