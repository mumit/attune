"""Fail-closed tests for the authenticated hosted worker dispatch core."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from uuid import UUID

from attune.hosted.repositories import HostedJob
from attune.hosted.worker_dispatch import TaskRoute, WorkerDispatcher

TENANT = UUID("10000000-0000-4000-8000-000000000001")
JOB_ID = UUID("10000000-0000-4000-8000-000000000071")
DELIVERY_ID = UUID("10000000-0000-4000-8000-000000000072")
AUDIENCE = "https://worker.example.test/tasks"
SERVICE_ACCOUNT = "attune-dispatch@example.iam.gserviceaccount.com"


def _body(*, purpose: str = "gmail.reconcile") -> bytes:
    return json.dumps(
        {
            "version": 1,
            "tenant_id": str(TENANT),
            "job_id": str(JOB_ID),
            "delivery_id": str(DELIVERY_ID),
            "purpose": purpose,
        }
    ).encode()


def _verifier(token: str, audience: str):
    now = int(time.time())
    assert token == "valid"
    assert audience == AUDIENCE
    return {
        "iss": "https://accounts.google.com",
        "aud": AUDIENCE,
        "email": SERVICE_ACCOUNT,
        "email_verified": True,
        "sub": "1234567890",
        "iat": now - 5,
        "exp": now + 300,
    }


class _Jobs:
    def __init__(self, job: HostedJob | None):
        self.job = job
        self.claims = []
        self.finished = []

    def claim(self, context, job_id, **kwargs):
        self.claims.append((context, job_id, kwargs))
        return self.job

    def finish(self, context, job_id, *, outcome):
        self.finished.append((context, job_id, outcome))
        return True


class _Audit:
    def __init__(self, *, fail: bool = False, fail_on: int | None = None):
        self.fail = fail
        self.fail_on = fail_on
        self.events = []

    def record(self, context, **event):
        if self.fail or self.fail_on == len(self.events) + 1:
            raise RuntimeError("audit unavailable")
        self.events.append((context, event))


class _Reconciliations:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.opened = []

    def open(self, context, job, *, reason_code):
        if self.fail:
            raise RuntimeError("reconciliation unavailable")
        self.opened.append((context, job, reason_code))
        return object()


def _job() -> HostedJob:
    return HostedJob(
        id=JOB_ID,
        kind="gmail.reconcile",
        state="leased",
        capability="gmail.read",
        payload={"canonical": "server-side"},
        attempts=1,
        available_at=datetime.now(timezone.utc),
        lease_expires_at=datetime.now(timezone.utc),
    )


def _dispatcher(jobs, audit, execute, reconciliations=None):
    route = TaskRoute("gmail.reconcile", "gmail.read", execute)
    return WorkerDispatcher(
        jobs=jobs,
        audit=audit,
        reconciliations=reconciliations or _Reconciliations(),
        routes={route.purpose: route},
        expected_audience=AUDIENCE,
        expected_service_account=SERVICE_ACCOUNT,
        token_verifier=_verifier,
    )


def test_invalid_envelope_never_reaches_storage_or_executor():
    jobs = _Jobs(_job())
    executed = []
    result = _dispatcher(jobs, _Audit(), executed.append).dispatch(
        authorization="not-a-bearer", raw_body=_body()
    )
    assert result.status_code == 403
    assert jobs.claims == []
    assert executed == []


def test_route_is_atomically_bound_to_kind_and_capability():
    jobs = _Jobs(_job())
    executed = []

    def execute(context, job):
        executed.append((context, job))

    audit = _Audit()
    result = _dispatcher(jobs, audit, execute).dispatch(
        authorization="Bearer valid", raw_body=_body()
    )
    assert result.status_code == 204
    assert jobs.claims[0][2] == {
        "expected_kind": "gmail.reconcile",
        "expected_capability": "gmail.read",
    }
    assert len(executed) == 1
    assert [event[1]["action"] for event in audit.events] == [
        "worker.job.claimed",
        "worker.job.execute",
    ]
    assert jobs.finished[-1][2] == "succeeded"


def test_duplicate_or_mismatched_delivery_has_no_effect():
    jobs = _Jobs(None)
    executed = []
    result = _dispatcher(jobs, _Audit(), executed.append).dispatch(
        authorization="Bearer valid", raw_body=_body()
    )
    assert result.status_code == 204
    assert executed == []
    assert jobs.finished == []


def test_audit_failure_prevents_effect_and_forces_reconciliation():
    jobs = _Jobs(_job())
    reconciliations = _Reconciliations()
    executed = []
    result = _dispatcher(
        jobs,
        _Audit(fail=True),
        executed.append,
        reconciliations,
    ).dispatch(
        authorization="Bearer valid", raw_body=_body()
    )
    assert result.status_code == 503
    assert executed == []
    assert reconciliations.opened[-1][2] == "pre_effect_audit"
    assert jobs.finished == []


def test_executor_failure_is_not_blindly_retried():
    jobs = _Jobs(_job())
    reconciliations = _Reconciliations()

    def fail(context, job):
        raise RuntimeError("ambiguous provider result")

    result = _dispatcher(jobs, _Audit(), fail, reconciliations).dispatch(
        authorization="Bearer valid", raw_body=_body()
    )
    assert result.status_code == 500
    assert reconciliations.opened[-1][2] == "executor_ambiguous"
    assert jobs.finished == []


def test_job_finalize_failure_opens_reconciliation():
    jobs = _Jobs(_job())
    jobs.finish = lambda *args, **kwargs: False
    reconciliations = _Reconciliations()
    result = _dispatcher(
        jobs,
        _Audit(),
        lambda context, job: None,
        reconciliations,
    ).dispatch(authorization="Bearer valid", raw_body=_body())
    assert result.status_code == 503
    assert reconciliations.opened[-1][2] == "job_finalize"


def test_post_effect_audit_failure_opens_reconciliation():
    jobs = _Jobs(_job())
    reconciliations = _Reconciliations()
    result = _dispatcher(
        jobs,
        _Audit(fail_on=2),
        lambda context, job: None,
        reconciliations,
    ).dispatch(authorization="Bearer valid", raw_body=_body())
    assert result.status_code == 503
    assert reconciliations.opened[-1][2] == "post_effect_audit"
    assert jobs.finished == []
