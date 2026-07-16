"""Opaque tenant-bound session primitives and PostgreSQL adapter."""

from __future__ import annotations

import base64
import hashlib
import secrets
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from .identity import VerifiedIdentity
from .repositories import ConnectionFactory
from .tenant import TenantContext

_SECRET_BYTES = 32
_BASE64URL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def _encode_secret(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def session_secret_hash(value: str) -> bytes:
    if (
        not isinstance(value, str)
        or len(value) != 43
        or any(character not in _BASE64URL for character in value)
    ):
        raise ValueError("invalid session secret")
    return hashlib.sha256(value.encode("ascii")).digest()


@dataclass(frozen=True, repr=False)
class IdentitySessionSecrets:
    token: str
    csrf: str

    def __repr__(self) -> str:
        return "IdentitySessionSecrets(<redacted>)"

    @property
    def token_hash(self) -> bytes:
        return session_secret_hash(self.token)

    @property
    def csrf_hash(self) -> bytes:
        return session_secret_hash(self.csrf)


@dataclass(frozen=True)
class IdentitySession:
    id: UUID
    context: TenantContext
    principal_id: UUID


def create_identity_session_secrets() -> IdentitySessionSecrets:
    return IdentitySessionSecrets(
        token=_encode_secret(secrets.token_bytes(_SECRET_BYTES)),
        csrf=_encode_secret(secrets.token_bytes(_SECRET_BYTES)),
    )


class PostgresIdentitySessionRepository:
    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def open(
        self,
        identity: VerifiedIdentity,
        secrets: IdentitySessionSecrets,
        *,
        expires_at: datetime,
    ) -> IdentitySession | None:
        if not isinstance(identity, VerifiedIdentity):
            raise TypeError("identity must be verified")
        if not isinstance(secrets, IdentitySessionSecrets):
            raise TypeError("session secrets are required")
        if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
            raise ValueError("session expiry must be timezone-aware")
        row = self._call_one(
            """
            SELECT session_id, tenant_id, principal_id
              FROM attune.open_identity_session(%s, %s, %s, %s, %s)
            """,
            (
                identity.subject_hash,
                identity.issuer,
                secrets.token_hash,
                secrets.csrf_hash,
                expires_at,
            ),
        )
        return _session(row)

    def read(self, token: str) -> IdentitySession | None:
        row = self._call_one(
            "SELECT session_id, tenant_id, principal_id "
            "FROM attune.read_identity_session(%s)",
            (session_secret_hash(token),),
        )
        return _session(row)

    def authorize(self, token: str, csrf: str) -> IdentitySession | None:
        row = self._call_one(
            "SELECT session_id, tenant_id, principal_id "
            "FROM attune.authorize_identity_session(%s, %s)",
            (session_secret_hash(token), session_secret_hash(csrf)),
        )
        return _session(row)

    def authorize_recent(self, token: str, csrf: str) -> IdentitySession | None:
        """Authorize a mutation only within ten minutes of web sign-in."""

        row = self._call_one(
            "SELECT session_id, tenant_id, principal_id "
            "FROM attune.authorize_recent_identity_session(%s, %s)",
            (session_secret_hash(token), session_secret_hash(csrf)),
        )
        return _session(row)

    def revoke(self, token: str, csrf: str) -> bool:
        row = self._call_one(
            "SELECT attune.revoke_identity_session(%s, %s)",
            (session_secret_hash(token), session_secret_hash(csrf)),
        )
        return bool(row and row[0])

    def _call_one(self, statement: str, parameters: tuple):
        with closing(self._connect()) as connection:
            try:
                with closing(connection.cursor()) as cursor:
                    cursor.execute(statement, parameters)
                    row = cursor.fetchone()
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return row


def _session(row) -> IdentitySession | None:
    if row is None:
        return None
    return IdentitySession(id=row[0], context=TenantContext(row[1]), principal_id=row[2])
