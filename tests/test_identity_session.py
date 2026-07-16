from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from attune.hosted.identity import VerifiedIdentity
from attune.hosted.identity_session import (
    IdentitySessionSecrets,
    PostgresIdentitySessionRepository,
    create_identity_session_secrets,
    session_secret_hash,
)

TENANT_ID = UUID("10000000-0000-4000-8000-000000000001")
PRINCIPAL_ID = UUID("20000000-0000-4000-8000-000000000001")
SESSION_ID = UUID("30000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


class Cursor:
    def __init__(self, row):
        self.row = row
        self.calls = []

    def execute(self, statement, parameters):
        self.calls.append((statement, parameters))

    def fetchone(self):
        return self.row

    def close(self):
        pass


class Connection:
    def __init__(self, row):
        self.cursor_value = Cursor(row)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


def test_session_secrets_are_independent_bounded_and_redacted():
    value = create_identity_session_secrets()
    assert len(value.token) == len(value.csrf) == 43
    assert value.token != value.csrf
    assert value.token not in repr(value)
    assert value.token_hash == hashlib.sha256(value.token.encode()).digest()
    assert value.csrf_hash == hashlib.sha256(value.csrf.encode()).digest()


@pytest.mark.parametrize("value", ["", "a" * 42, "a" * 44, "+" * 43])
def test_session_hash_rejects_noncanonical_values(value):
    with pytest.raises(ValueError):
        session_secret_hash(value)


def test_repository_opens_only_from_verified_identity_and_hashes_secrets():
    connection = Connection((SESSION_ID, TENANT_ID, PRINCIPAL_ID))
    repository = PostgresIdentitySessionRepository(lambda: connection)
    identity = VerifiedIdentity(
        issuer="https://securetoken.google.com/attune-development-502421",
        subject_hash=bytes.fromhex("11" * 32),
        authenticated_at=NOW,
    )
    secrets = IdentitySessionSecrets(token="a" * 43, csrf="b" * 43)
    session = repository.open(identity, secrets, expires_at=NOW + timedelta(hours=8))
    assert session.id == SESSION_ID
    assert session.context.tenant_id == TENANT_ID
    assert session.principal_id == PRINCIPAL_ID
    parameters = connection.cursor_value.calls[0][1]
    assert parameters[0] == identity.subject_hash
    assert parameters[2] == session_secret_hash(secrets.token)
    assert parameters[3] == session_secret_hash(secrets.csrf)
    assert secrets.token not in repr(parameters)
    assert connection.commits == 1


def test_repository_fails_closed_for_missing_or_ambiguous_membership():
    connection = Connection(None)
    repository = PostgresIdentitySessionRepository(lambda: connection)
    identity = VerifiedIdentity(
        issuer="https://securetoken.google.com/attune-development-502421",
        subject_hash=bytes.fromhex("22" * 32),
        authenticated_at=NOW,
    )
    assert (
        repository.open(
            identity,
            IdentitySessionSecrets(token="a" * 43, csrf="b" * 43),
            expires_at=NOW + timedelta(hours=8),
        )
        is None
    )


def test_repository_authorize_and_revoke_require_both_secrets():
    connection = Connection((SESSION_ID, TENANT_ID, PRINCIPAL_ID))
    repository = PostgresIdentitySessionRepository(lambda: connection)
    assert repository.authorize("a" * 43, "b" * 43).id == SESSION_ID
    assert repository.authorize_recent("a" * 43, "b" * 43).id == SESSION_ID
    statement, parameters = connection.cursor_value.calls[0]
    assert "authorize_identity_session" in statement
    assert parameters == (
        session_secret_hash("a" * 43),
        session_secret_hash("b" * 43),
    )
    assert "authorize_recent_identity_session" in connection.cursor_value.calls[1][0]

    revoked_connection = Connection((True,))
    revoked = PostgresIdentitySessionRepository(lambda: revoked_connection)
    assert revoked.revoke("a" * 43, "b" * 43) is True
