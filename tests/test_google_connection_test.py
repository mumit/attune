from datetime import datetime, timezone
from uuid import UUID

from attune.hosted.google_connection_test import (
    CAPABILITY,
    REQUIRED_SCOPES,
    GoogleWorkspaceConnectionTest,
)
from attune.hosted.repositories import HostedJob
from attune.hosted.tenant import TenantContext

TENANT = TenantContext(UUID("10000000-0000-4000-8000-000000000001"))
PRINCIPAL = UUID("20000000-0000-4000-8000-000000000001")
CONNECTOR = UUID("30000000-0000-4000-8000-000000000001")
JOB = UUID("40000000-0000-4000-8000-000000000001")
INTENT = UUID("50000000-0000-4000-8000-000000000001")


class Connectors:
    def __init__(self, connector=CONNECTOR):
        self.connector = connector
        self.calls = []

    def active_connector(self, context, *, principal_id, required_scopes):
        self.calls.append((context, principal_id, required_scopes))
        return self.connector


class Dispatches:
    def __init__(self):
        self.calls = []

    def enqueue(self, context, **kwargs):
        self.calls.append((context, kwargs))
        job = hosted_job("queued")
        intent = type("Intent", (), {"id": INTENT})()
        return type("Dispatch", (), {"job": job, "intent": intent})()


class Jobs:
    def __init__(self, job):
        self.job = job

    def get(self, context, job_id):
        assert context == TENANT and job_id == JOB
        return self.job


class Broker:
    def __init__(self, accepted=True):
        self.accepted = accepted
        self.calls = []

    def dispatch(self, intent_id):
        self.calls.append(intent_id)
        return self.accepted


def hosted_job(state, payload=None):
    return HostedJob(
        JOB,
        CAPABILITY,
        state,
        CAPABILITY,
        payload or {"connector_id": str(CONNECTOR)},
        0,
        datetime.now(timezone.utc),
        None,
    )


def test_start_derives_fixed_capability_and_connector_server_side():
    connectors = Connectors()
    dispatches = Dispatches()
    broker = Broker()
    service = GoogleWorkspaceConnectionTest(
        connectors, dispatches, Jobs(hosted_job("queued")), broker
    )
    started = service.start(TENANT, principal_id=PRINCIPAL)
    assert started.job_id == JOB and started.state == "queued"
    assert connectors.calls == [(TENANT, PRINCIPAL, REQUIRED_SCOPES)]
    context, values = dispatches.calls[0]
    assert context == TENANT
    assert values["kind"] == CAPABILITY
    assert values["capability"] == CAPABILITY
    assert values["payload"] == {"connector_id": str(CONNECTOR)}
    assert len(values["idempotency_key"]) == 32
    assert broker.calls == [INTENT]


def test_status_is_principal_connector_bound_and_data_minimized():
    for stored, public in (
        ("queued", "queued"),
        ("leased", "running"),
        ("succeeded", "succeeded"),
        ("failed", "failed"),
        ("reconcile", "failed"),
        ("cancelled", "failed"),
    ):
        service = GoogleWorkspaceConnectionTest(
            Connectors(), Dispatches(), Jobs(hosted_job(stored)), Broker()
        )
        assert service.status(TENANT, principal_id=PRINCIPAL, job_id=JOB) == public

    wrong = hosted_job("succeeded", {"connector_id": str(UUID(int=9))})
    service = GoogleWorkspaceConnectionTest(
        Connectors(), Dispatches(), Jobs(wrong), Broker()
    )
    assert service.status(TENANT, principal_id=PRINCIPAL, job_id=JOB) is None
