from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from types import MappingProxyType
from uuid import UUID

import pytest

from attune.hosted.capability_admission import (
    CapabilityAdmissionProducer,
    PostgresCapabilityAdmissionRepository,
    RecordedAdmission,
)
from attune.hosted.capability_gateway import AuthorizedCapability, RiskTier
from attune.hosted.dispatch import EnqueuedDispatch, HostedDispatchIntent
from attune.hosted.repositories import ClaimedApproval, HostedJob
from attune.hosted.tenant import TenantContext

TENANT = TenantContext(UUID("10000000-0000-4000-8000-000000000751"))
PRINCIPAL = UUID("10000000-0000-4000-8000-000000000752")
CONNECTOR = UUID("10000000-0000-4000-8000-000000000753")
APPROVAL = UUID("10000000-0000-4000-8000-000000000754")
ADMISSION = UUID("10000000-0000-4000-8000-000000000755")
JOB = UUID("10000000-0000-4000-8000-000000000756")
DISPATCH_INTENT = UUID("10000000-0000-4000-8000-000000000757")
DELIVERY = UUID("10000000-0000-4000-8000-000000000758")
NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


def authorized(**overrides) -> AuthorizedCapability:
    values = dict(
        context=TENANT,
        principal_id=PRINCIPAL,
        connector_id=CONNECTOR,
        capability="google.gmail.draft.create",
        contract_version=1,
        risk=RiskTier.R2,
        policy_version=7,
        arguments=MappingProxyType({"thread_ref": "thread_1", "body": "Hello"}),
    )
    values.update(overrides)
    return AuthorizedCapability(**values)


class FakeAdmissions:
    def __init__(self):
        self.calls = []

    def record(self, context, *, authorized, destination_hash, now):
        self.calls.append((context, authorized, destination_hash, now))
        return RecordedAdmission(admission_id=ADMISSION, approval_id=APPROVAL)


class FakeApprovals:
    def __init__(self, result="not_configured"):
        self._results = result if isinstance(result, list) else [result]
        self.calls = []

    def claim(self, context, *, approval_id, principal_id, decision):
        self.calls.append((context, approval_id, principal_id, decision))
        outcome = self._results.pop(0) if len(self._results) > 1 else self._results[0]
        if outcome is None:
            return None
        return outcome


class FakeDispatch:
    def __init__(self):
        self.calls = []

    def enqueue(self, context, **kwargs):
        self.calls.append((context, kwargs))
        return EnqueuedDispatch(
            job=HostedJob(JOB, kwargs["kind"], "queued", kwargs["capability"], kwargs["payload"], 0, NOW, None),
            intent=HostedDispatchIntent(
                DISPATCH_INTENT, JOB, DELIVERY, "worker", kwargs["kind"],
                kwargs["capability"], "requested", 0, kwargs["expires_at"],
            ),
        )


class FakeBroker:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def dispatch(self, intent_id):
        self.calls.append(intent_id)
        return self.ok


def consumed_claim(**overrides) -> ClaimedApproval:
    values = dict(
        approval_id=APPROVAL,
        admission_id=ADMISSION,
        job_id=None,
        capability="google.gmail.draft.create",
        arguments={"thread_ref": "thread_1", "body": "Hello"},
        connector_id=CONNECTOR,
        policy_version=7,
        final_status="consumed",
    )
    values.update(overrides)
    return ClaimedApproval(**values)


def test_record_never_touches_approvals_dispatch_or_broker():
    admissions, approvals, dispatch, broker = (
        FakeAdmissions(), FakeApprovals(), FakeDispatch(), FakeBroker()
    )
    producer = CapabilityAdmissionProducer(
        admissions, approvals, dispatch, broker, now=lambda: NOW
    )
    destination_hash = hashlib.sha256(b"thread_1").digest()
    recorded = producer.record(
        TENANT, authorized=authorized(), destination_hash=destination_hash
    )
    assert recorded == RecordedAdmission(admission_id=ADMISSION, approval_id=APPROVAL)
    assert admissions.calls == [(TENANT, authorized(), destination_hash, NOW)]
    assert approvals.calls == []
    assert dispatch.calls == []
    assert broker.calls == []


def test_decide_rejected_never_dispatches():
    approvals = FakeApprovals(
        ClaimedApproval(
            approval_id=APPROVAL, admission_id=ADMISSION, job_id=None,
            capability=None, arguments=None, connector_id=None,
            policy_version=None, final_status="rejected",
        )
    )
    dispatch, broker = FakeDispatch(), FakeBroker()
    producer = CapabilityAdmissionProducer(
        FakeAdmissions(), approvals, dispatch, broker, now=lambda: NOW
    )
    status = producer.decide(
        TENANT, approval_id=APPROVAL, principal_id=PRINCIPAL, decision="rejected",
    )
    assert status == "rejected"
    assert dispatch.calls == []
    assert broker.calls == []


def test_decide_not_found_never_dispatches():
    producer = CapabilityAdmissionProducer(
        FakeAdmissions(), FakeApprovals(None), FakeDispatch(), FakeBroker(), now=lambda: NOW
    )
    status = producer.decide(
        TENANT, approval_id=APPROVAL, principal_id=PRINCIPAL, decision="approved",
    )
    assert status == "not_found"


def test_decide_expired_never_dispatches():
    approvals = FakeApprovals(consumed_claim(final_status="expired", capability=None, arguments=None, connector_id=None, policy_version=None))
    dispatch, broker = FakeDispatch(), FakeBroker()
    producer = CapabilityAdmissionProducer(
        FakeAdmissions(), approvals, dispatch, broker, now=lambda: NOW
    )
    status = producer.decide(
        TENANT, approval_id=APPROVAL, principal_id=PRINCIPAL, decision="approved",
    )
    assert status == "expired"
    assert dispatch.calls == []
    assert broker.calls == []


def test_decide_consumed_dispatches_through_the_existing_producer():
    approvals = FakeApprovals(consumed_claim())
    dispatch, broker = FakeDispatch(), FakeBroker()
    producer = CapabilityAdmissionProducer(
        FakeAdmissions(), approvals, dispatch, broker, now=lambda: NOW
    )
    status = producer.decide(
        TENANT, approval_id=APPROVAL, principal_id=PRINCIPAL, decision="approved",
    )
    assert status == "consumed"
    assert len(dispatch.calls) == 1
    context, kwargs = dispatch.calls[0]
    assert context == TENANT
    assert kwargs["kind"] == "google.gmail.draft.create"
    assert kwargs["capability"] == "google.gmail.draft.create"
    assert kwargs["payload"] == {
        "schema_version": 1,
        "admission_id": str(ADMISSION),
        "connector_id": str(CONNECTOR),
        "thread_ref": "thread_1",
        "body": "Hello",
    }
    assert len(kwargs["idempotency_key"]) == 32
    assert broker.calls == [DISPATCH_INTENT]


def test_decide_is_idempotent_on_replay_and_dispatches_only_via_idempotent_producer():
    """A replayed claim (already consumed) is expected to call decide()
    again in production only if a caller retries; the underlying dispatch
    producer is itself idempotent (same derived idempotency key), so
    calling decide() twice for the same approval never creates two jobs --
    this test pins that decide() always attempts the (idempotent) dispatch
    call whenever the claim reports consumed, replay or not."""
    approvals = FakeApprovals(consumed_claim())
    dispatch, broker = FakeDispatch(), FakeBroker()
    producer = CapabilityAdmissionProducer(
        FakeAdmissions(), approvals, dispatch, broker, now=lambda: NOW
    )
    first = producer.decide(
        TENANT, approval_id=APPROVAL, principal_id=PRINCIPAL, decision="approved",
    )
    second = producer.decide(
        TENANT, approval_id=APPROVAL, principal_id=PRINCIPAL, decision="approved",
    )
    assert first == second == "consumed"
    assert dispatch.calls[0][1]["idempotency_key"] == dispatch.calls[1][1]["idempotency_key"]


def test_decide_raises_when_broker_dispatch_is_refused():
    approvals = FakeApprovals(consumed_claim())
    producer = CapabilityAdmissionProducer(
        FakeAdmissions(), approvals, FakeDispatch(), FakeBroker(ok=False), now=lambda: NOW
    )
    with pytest.raises(RuntimeError):
        producer.decide(
            TENANT, approval_id=APPROVAL, principal_id=PRINCIPAL, decision="approved",
        )


def test_record_rejects_non_capability_and_wrong_hash_shape():
    repository = PostgresCapabilityAdmissionRepository(lambda: None)
    with pytest.raises(TypeError):
        repository.record(
            TENANT, authorized="not-a-capability", destination_hash=b"x" * 32, now=NOW,
        )
    with pytest.raises(ValueError):
        repository.record(
            TENANT, authorized=authorized(), destination_hash=b"short", now=NOW,
        )
    with pytest.raises(ValueError):
        repository.record(
            TENANT, authorized=authorized(), destination_hash=b"x" * 32,
            now=datetime(2026, 7, 19, 12, 0),
        )
