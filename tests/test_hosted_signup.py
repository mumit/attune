from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from attune.hosted.hosted_signup import (
    HOSTED_SIGNUP_THROTTLE_LIMIT,
    HOSTED_SIGNUP_THROTTLE_WINDOW,
    HostedSignupService,
    PostgresHostedSignupRepository,
    SignupThrottle,
)
from attune.hosted.identity import VerifiedIdentity
from attune.hosted.tenant import TenantContext

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
TENANT_ID = UUID("10000000-0000-4000-8000-000000000001")
PRINCIPAL_ID = UUID("20000000-0000-4000-8000-000000000001")
ISSUER = "https://securetoken.google.com/attune-development-502421"


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


class FakeAuditIntent:
    def __init__(self, id):
        self.id = id


class FakeAudit:
    def __init__(self):
        self.calls = []

    def request(self, context, **kwargs):
        self.calls.append((context, kwargs))
        return FakeAuditIntent(UUID(int=1))


class FakeWriter:
    def __init__(self, written=True):
        self.written = written
        self.calls = []

    def write(self, audit_intent_id):
        self.calls.append(audit_intent_id)
        return self.written


class FakeProvisioner:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def provision(self, subject_hash, issuer, *, region):
        self.calls.append((subject_hash, issuer, region))
        return self.result


def identity(subject=b"1" * 32) -> VerifiedIdentity:
    return VerifiedIdentity(issuer=ISSUER, subject_hash=subject, authenticated_at=NOW)


# --- SignupThrottle: throttle math -----------------------------------------


def test_throttle_allows_up_to_the_limit_then_denies_within_the_window():
    throttle = SignupThrottle(limit=3, window=timedelta(seconds=60))
    key = b"subject:" + b"a" * 32
    assert [throttle.allow(key, now=NOW) for _ in range(3)] == [True, True, True]
    assert throttle.allow(key, now=NOW) is False
    assert throttle.allow(key, now=NOW + timedelta(seconds=30)) is False


def test_throttle_resets_once_the_window_elapses():
    throttle = SignupThrottle(limit=1, window=timedelta(seconds=60))
    key = b"subject:" + b"a" * 32
    assert throttle.allow(key, now=NOW) is True
    assert throttle.allow(key, now=NOW + timedelta(seconds=59)) is False
    assert throttle.allow(key, now=NOW + timedelta(seconds=61)) is True


def test_throttle_tracks_keys_independently():
    throttle = SignupThrottle(limit=1, window=timedelta(seconds=60))
    assert throttle.allow(b"subject:a", now=NOW) is True
    assert throttle.allow(b"subject:b", now=NOW) is True
    assert throttle.allow(b"subject:a", now=NOW) is False


def test_throttle_rejects_invalid_construction_and_calls():
    with pytest.raises(ValueError):
        SignupThrottle(limit=0)
    with pytest.raises(ValueError):
        SignupThrottle(window=timedelta(0))
    throttle = SignupThrottle()
    with pytest.raises(TypeError):
        throttle.allow(b"", now=NOW)
    with pytest.raises(TypeError):
        throttle.allow("not-bytes", now=NOW)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        throttle.allow(b"k", now=datetime(2026, 1, 1))


def test_default_throttle_constants_mirror_the_edge_ceremony_rule():
    # docs/hosted-signup.md section 7: the in-process backstop is meant to
    # agree with the 10-per-60-second Cloud Armor onboarding-ceremony rule.
    assert HOSTED_SIGNUP_THROTTLE_LIMIT == 10
    assert HOSTED_SIGNUP_THROTTLE_WINDOW == timedelta(seconds=60)


# --- PostgresHostedSignupRepository: function-invocation shape -------------


def test_repository_invokes_exactly_the_signup_function_with_ordered_arguments():
    connection = Connection((str(TENANT_ID), str(PRINCIPAL_ID), True))
    repository = PostgresHostedSignupRepository(lambda: connection)
    subject_hash = hashlib.sha256(b"a-subject").digest()

    tenant_id, principal_id, created = repository.provision(
        subject_hash, ISSUER, region="northamerica-northeast1"
    )

    assert (tenant_id, principal_id, created) == (TENANT_ID, PRINCIPAL_ID, True)
    statement, parameters = connection.cursor_value.calls[0]
    assert "provision_hosted_signup_tenant" in statement
    assert parameters == (subject_hash, ISSUER, "northamerica-northeast1")
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_repository_rejects_invalid_input_before_connecting():
    repository = PostgresHostedSignupRepository(lambda: (_ for _ in ()).throw(AssertionError))
    with pytest.raises(ValueError):
        repository.provision(b"short", ISSUER, region="test")
    with pytest.raises(ValueError):
        repository.provision(b"1" * 32, "", region="test")
    with pytest.raises(ValueError):
        repository.provision(b"1" * 32, ISSUER, region="")


def test_repository_rolls_back_and_reraises_on_failure():
    class BrokenCursor(Cursor):
        def execute(self, statement, parameters):
            raise RuntimeError("boom")

    class BrokenConnection(Connection):
        def cursor(self):
            return BrokenCursor(None)

    connection = BrokenConnection(None)
    repository = PostgresHostedSignupRepository(lambda: connection)
    with pytest.raises(RuntimeError):
        repository.provision(b"1" * 32, ISSUER, region="test")
    assert connection.rollbacks == 1
    assert connection.commits == 0


# --- HostedSignupService: idempotent outcomes + content-free audit ---------


def test_signup_service_reports_created_and_writes_content_free_audit():
    provisioner = FakeProvisioner((TENANT_ID, PRINCIPAL_ID, True))
    audit = FakeAudit()
    writer = FakeWriter()
    service = HostedSignupService(provisioner, audit, writer, region="test-region")

    result = service.provision(identity())

    assert result.status == "created"
    assert result.tenant_id == TENANT_ID
    assert result.principal_id == PRINCIPAL_ID
    assert provisioner.calls == [(identity().subject_hash, ISSUER, "test-region")]
    context, kwargs = audit.calls[0]
    assert context == TenantContext(TENANT_ID)
    assert kwargs["actor_type"] == "principal"
    assert kwargs["actor_ref_hash"] == identity().subject_hash
    assert kwargs["action"] == "hosted_signup.provision"
    assert kwargs["outcome"] == "observed"
    assert kwargs["metadata"] == {"created": True}
    # Content-free: no email, no raw subject, no tenant slug anywhere in the
    # audit call arguments.
    serialized = repr(kwargs)
    assert "@" not in serialized
    assert "tn-" not in serialized


def test_signup_service_reports_already_provisioned_idempotently():
    provisioner = FakeProvisioner((TENANT_ID, PRINCIPAL_ID, False))
    audit = FakeAudit()
    writer = FakeWriter()
    service = HostedSignupService(provisioner, audit, writer, region="test-region")

    first = service.provision(identity())
    second = service.provision(identity())

    assert first.status == second.status == "already_provisioned"
    assert first.tenant_id == second.tenant_id == TENANT_ID
    assert first.principal_id == second.principal_id == PRINCIPAL_ID
    # Same idempotency key both times (the underlying repository dedupes it).
    first_key = audit.calls[0][1]["idempotency_key"]
    second_key = audit.calls[1][1]["idempotency_key"]
    assert first_key == second_key


def test_signup_service_distinguishes_created_from_replay_idempotency_keys():
    provisioner = FakeProvisioner((TENANT_ID, PRINCIPAL_ID, True))
    audit = FakeAudit()
    service = HostedSignupService(provisioner, audit, FakeWriter(), region="test-region")
    created = service.provision(identity())

    provisioner.result = (TENANT_ID, PRINCIPAL_ID, False)
    replay = service.provision(identity())

    created_key = audit.calls[0][1]["idempotency_key"]
    replay_key = audit.calls[1][1]["idempotency_key"]
    assert created_key != replay_key
    assert created.status == "created"
    assert replay.status == "already_provisioned"


def test_signup_service_requires_audit_write_to_succeed():
    provisioner = FakeProvisioner((TENANT_ID, PRINCIPAL_ID, True))
    service = HostedSignupService(
        provisioner, FakeAudit(), FakeWriter(written=False), region="test-region"
    )
    with pytest.raises(RuntimeError, match="audit"):
        service.provision(identity())


def test_signup_service_rejects_unverified_input():
    service = HostedSignupService(
        FakeProvisioner((TENANT_ID, PRINCIPAL_ID, True)),
        FakeAudit(),
        FakeWriter(),
        region="test-region",
    )
    with pytest.raises(TypeError):
        service.provision("not-an-identity")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        HostedSignupService(
            FakeProvisioner((TENANT_ID, PRINCIPAL_ID, True)),
            FakeAudit(),
            FakeWriter(),
            region="",
        )
