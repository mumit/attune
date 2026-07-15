from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID

from attune.hosted.dispatch import LeasedDispatch
from attune.hosted.dispatch_broker import (
    BrokerRoute,
    DispatchBroker,
    TaskAlreadyExists,
)
from attune.hosted.tenant import TenantContext

INTENT = UUID("10000000-0000-4000-8000-000000000101")
TENANT = UUID("10000000-0000-4000-8000-000000000102")
JOB = UUID("10000000-0000-4000-8000-000000000103")
DELIVERY = UUID("10000000-0000-4000-8000-000000000104")
URL = "https://worker.example.run.app"
QUEUE = "projects/test/locations/test/queues/jobs"


def leased(state="leased", purpose="platform.smoke"):
    return LeasedDispatch(
        INTENT,
        TenantContext(TENANT),
        JOB,
        DELIVERY,
        purpose,
        "platform.smoke",
        state,
        1,
        datetime.now(timezone.utc),
    )


class Intents:
    def __init__(self, value):
        self.value = value
        self.finalized = []

    def lease(self, *args, **kwargs):
        return self.value

    def finalize(self, intent_id, **kwargs):
        self.finalized.append((intent_id, kwargs))
        return True


class Tasks:
    def __init__(self, error=None):
        self.error = error
        self.created = []

    def create(self, route, dispatch, body):
        self.created.append((route, dispatch, body))
        if self.error:
            raise self.error


class Audit:
    def __init__(self, results=(True, True)):
        self.results = iter(results)
        self.events = []

    def record(self, intent_id, **kwargs):
        self.events.append((intent_id, kwargs))
        return next(self.results)


def broker(intents, tasks, audit, routes=None):
    route = BrokerRoute("platform.smoke", QUEUE, URL, URL)
    return DispatchBroker(
        intents=intents,
        tasks=tasks,
        audit=audit,
        routes={route.purpose: route} if routes is None else routes,
    )


def test_broker_audits_before_task_and_uses_canonical_envelope():
    intents = Intents(leased())
    tasks = Tasks()
    audit = Audit()
    result = broker(intents, tasks, audit).dispatch(
        INTENT, producer_kind="control_plane"
    )

    assert result.status_code == 204
    assert [event[1]["outcome"] for event in audit.events] == [
        "allowed",
        "observed",
    ]
    assert intents.finalized[0][1]["outcome"] == "dispatched"
    body = json.loads(tasks.created[0][2])
    assert body == {
        "version": 1,
        "tenant_id": str(TENANT),
        "job_id": str(JOB),
        "delivery_id": str(DELIVERY),
        "purpose": "platform.smoke",
    }


def test_broker_creates_no_task_when_pre_audit_fails():
    intents = Intents(leased())
    tasks = Tasks()
    result = broker(intents, tasks, Audit((False,))).dispatch(
        INTENT, producer_kind="worker"
    )
    assert result.status_code == 503
    assert tasks.created == []
    assert intents.finalized == []


def test_broker_treats_deterministic_already_exists_as_success():
    intents = Intents(leased())
    tasks = Tasks(TaskAlreadyExists())
    result = broker(intents, tasks, Audit()).dispatch(
        INTENT, producer_kind="worker"
    )
    assert result.status_code == 204
    assert intents.finalized[0][1]["outcome"] == "dispatched"


def test_broker_refuses_unregistered_route_and_audits_failure():
    intents = Intents(leased(purpose="unknown.route"))
    tasks = Tasks()
    audit = Audit((True,))
    result = broker(intents, tasks, audit).dispatch(
        INTENT, producer_kind="worker"
    )
    assert result.status_code == 403
    assert tasks.created == []
    assert intents.finalized[0][1]["outcome"] == "failed"
    assert audit.events[0][1] == {
        "outcome": "failed",
        "error_code": "route_not_registered",
    }


def test_broker_replays_only_post_dispatch_audit():
    intents = Intents(leased(state="dispatched"))
    tasks = Tasks()
    audit = Audit((True,))
    result = broker(intents, tasks, audit).dispatch(
        INTENT, producer_kind="control_plane"
    )
    assert result.status_code == 204
    assert tasks.created == []
    assert audit.events[0][1]["outcome"] == "observed"
