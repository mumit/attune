from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from attune.hosted.brief_delivery import CAPABILITY, PURPOSE
from attune.hosted.brief_producer import HostedBriefProducer
from attune.hosted.dispatch import EnqueuedDispatch, HostedDispatchIntent
from attune.hosted.repositories import HostedJob
from attune.hosted.tenant import TenantContext

TENANT = TenantContext(UUID("30000000-0000-4000-8000-000000000001"))
PRINCIPAL = UUID("30000000-0000-4000-8000-000000000002")
JOB = UUID("30000000-0000-4000-8000-000000000003")
INTENT = UUID("30000000-0000-4000-8000-000000000004")
NOW = datetime(2026, 7, 19, 8, 30, tzinfo=timezone.utc)


def _hosted_job(now):
    return HostedJob(
        JOB, PURPOSE, "queued", CAPABILITY,
        {"schema_version": 1, "principal_id": str(PRINCIPAL)},
        0, now, None,
    )


class FakeDispatches:
    def __init__(self):
        self.calls = []

    def enqueue(self, context, *, kind, capability, payload, idempotency_key, expires_at):
        self.calls.append({
            "context": context, "kind": kind, "capability": capability,
            "payload": payload, "idempotency_key": idempotency_key,
            "expires_at": expires_at,
        })
        intent = HostedDispatchIntent(
            INTENT, JOB, UUID(int=1), "control_plane", kind, capability,
            "queued", 0, expires_at,
        )
        return EnqueuedDispatch(_hosted_job(expires_at), intent)


class FakeBroker:
    def __init__(self, accepted=True):
        self.accepted = accepted
        self.calls = []

    def dispatch(self, intent_id):
        self.calls.append(intent_id)
        return self.accepted


def test_run_enqueues_the_fixed_brief_job_and_dispatches():
    dispatches = FakeDispatches()
    broker = FakeBroker()
    producer = HostedBriefProducer(dispatches, broker, now=lambda: NOW)
    started = producer.run(TENANT, principal_id=PRINCIPAL)
    assert started.job_id == JOB
    call = dispatches.calls[0]
    assert call["kind"] == "channel.brief.deliver"
    assert call["capability"] == "assistant.brief.deliver"
    assert call["payload"] == {"schema_version": 1, "principal_id": str(PRINCIPAL)}
    assert broker.calls == [INTENT]


def test_idempotency_key_is_identical_within_the_same_utc_hour():
    """Deliverable 2: idempotent per tenant per principal per UTC hour."""
    dispatches = FakeDispatches()
    broker = FakeBroker()
    producer = HostedBriefProducer(dispatches, broker, now=lambda: NOW)
    producer.run(TENANT, principal_id=PRINCIPAL)
    producer.run(TENANT, principal_id=PRINCIPAL)
    keys = [call["idempotency_key"] for call in dispatches.calls]
    assert keys[0] == keys[1]


def test_idempotency_key_changes_in_the_next_utc_hour():
    dispatches = FakeDispatches()
    broker = FakeBroker()
    clock = {"now": NOW}
    producer = HostedBriefProducer(dispatches, broker, now=lambda: clock["now"])
    producer.run(TENANT, principal_id=PRINCIPAL)
    clock["now"] = NOW + timedelta(hours=1)
    producer.run(TENANT, principal_id=PRINCIPAL)
    keys = [call["idempotency_key"] for call in dispatches.calls]
    assert keys[0] != keys[1]


def test_idempotency_key_is_scoped_per_tenant_and_principal():
    dispatches = FakeDispatches()
    broker = FakeBroker()
    producer = HostedBriefProducer(dispatches, broker, now=lambda: NOW)
    producer.run(TENANT, principal_id=PRINCIPAL)
    other_tenant = TenantContext(UUID(int=999))
    producer.run(other_tenant, principal_id=PRINCIPAL)
    other_principal = UUID(int=888)
    producer.run(TENANT, principal_id=other_principal)
    keys = [call["idempotency_key"] for call in dispatches.calls]
    assert len(set(keys)) == 3


def test_dispatch_refusal_raises():
    dispatches = FakeDispatches()
    broker = FakeBroker(accepted=False)
    producer = HostedBriefProducer(dispatches, broker, now=lambda: NOW)
    with pytest.raises(RuntimeError, match="dispatch was refused"):
        producer.run(TENANT, principal_id=PRINCIPAL)


def test_naive_clock_is_rejected():
    dispatches = FakeDispatches()
    broker = FakeBroker()
    producer = HostedBriefProducer(
        dispatches, broker, now=lambda: datetime(2026, 7, 19, 8, 30)
    )
    with pytest.raises(RuntimeError, match="timezone-aware"):
        producer.run(TENANT, principal_id=PRINCIPAL)


def test_rejects_untrusted_context_or_principal_types():
    dispatches = FakeDispatches()
    broker = FakeBroker()
    producer = HostedBriefProducer(dispatches, broker, now=lambda: NOW)
    with pytest.raises(TypeError):
        producer.run("not-a-context", principal_id=PRINCIPAL)
    with pytest.raises(TypeError):
        producer.run(TENANT, principal_id="not-a-uuid")
