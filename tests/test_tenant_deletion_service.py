from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from attune.hosted.audit import HostedAuditIntent
from attune.hosted.tenant import TenantContext
from attune.hosted.tenant_deletion import (
    TenantDeletionCancellation,
    TenantDeletionRequest,
)
from attune.hosted.tenant_deletion_service import TenantDeletionService

TENANT = TenantContext(UUID("10000000-0000-4000-8000-000000000001"))
PRINCIPAL = UUID("10000000-0000-4000-8000-000000000002")
SESSION = UUID("10000000-0000-4000-8000-000000000003")


class Requests:
    def __init__(self, created=True, cancelled=True, error=None, status="none"):
        self.created = created
        self.cancelled = cancelled
        self.error = error
        self.status_value = status
        self.calls = []

    def request(self, context, *, principal_id, session_id):
        self.calls.append(("request", context, principal_id, session_id))
        if self.error:
            raise self.error
        now = datetime.now(timezone.utc)
        return TenantDeletionRequest(
            UUID(int=1), "pending", now, now + timedelta(days=14), self.created
        )

    def cancel(self, context, *, principal_id, session_id):
        self.calls.append(("cancel", context, principal_id, session_id))
        if self.error:
            raise self.error
        return TenantDeletionCancellation(
            self.cancelled, "cancelled" if self.cancelled else "claimed"
        )

    def read(self, context, *, principal_id):
        self.calls.append(("read", context, principal_id))
        return self.status_value


class Audit:
    def __init__(self):
        self.calls = []

    def request(self, context, **kwargs):
        self.calls.append((context, kwargs))
        return HostedAuditIntent(
            UUID(int=len(self.calls)),
            "control_plane",
            kwargs["action"],
            kwargs["outcome"],
            "pending",
            None,
        )


class Writer:
    def __init__(self, results=(True, True)):
        self.results = iter(results)
        self.calls = []

    def write(self, intent_id):
        self.calls.append(intent_id)
        return next(self.results)


def test_request_requires_pre_effect_and_records_observed_outcome():
    requests, audit, writer = Requests(), Audit(), Writer()
    result = TenantDeletionService(requests, audit, writer).request(
        TENANT, principal_id=PRINCIPAL, session_id=SESSION
    )
    assert result.status == "pending"
    assert requests.calls == [("request", TENANT, PRINCIPAL, SESSION)]
    assert [call[1]["outcome"] for call in audit.calls] == ["allowed", "observed"]
    assert audit.calls[0][1]["idempotency_key"] != audit.calls[1][1]["idempotency_key"]
    assert all(call[1]["actor_ref_hash"] for call in audit.calls)


def test_request_pre_effect_audit_failure_prevents_mutation():
    requests, audit, writer = Requests(), Audit(), Writer((False,))
    with pytest.raises(RuntimeError, match="pre-effect audit"):
        TenantDeletionService(requests, audit, writer).request(
            TENANT, principal_id=PRINCIPAL, session_id=SESSION
        )
    assert requests.calls == []


def test_request_database_refusal_is_followed_by_failed_audit():
    requests = Requests(error=RuntimeError("recent session expired"))
    audit, writer = Audit(), Writer()
    with pytest.raises(RuntimeError, match="recent session"):
        TenantDeletionService(requests, audit, writer).request(
            TENANT, principal_id=PRINCIPAL, session_id=SESSION
        )
    assert [call[1]["outcome"] for call in audit.calls] == ["allowed", "failed"]


def test_request_outcome_audit_failure_is_visible_after_idempotent_effect():
    requests, audit, writer = Requests(), Audit(), Writer((True, False))
    with pytest.raises(RuntimeError, match="outcome audit"):
        TenantDeletionService(requests, audit, writer).request(
            TENANT, principal_id=PRINCIPAL, session_id=SESSION
        )
    assert requests.calls == [("request", TENANT, PRINCIPAL, SESSION)]


def test_cancel_records_observed_when_it_succeeds():
    requests, audit, writer = Requests(cancelled=True), Audit(), Writer()
    result = TenantDeletionService(requests, audit, writer).cancel(
        TENANT, principal_id=PRINCIPAL, session_id=SESSION
    )
    assert result.cancelled is True
    assert [call[1]["outcome"] for call in audit.calls] == ["allowed", "observed"]


def test_cancel_records_failed_when_nothing_was_cancellable():
    requests, audit, writer = Requests(cancelled=False), Audit(), Writer()
    result = TenantDeletionService(requests, audit, writer).cancel(
        TENANT, principal_id=PRINCIPAL, session_id=SESSION
    )
    assert result.cancelled is False
    assert [call[1]["outcome"] for call in audit.calls] == ["allowed", "failed"]


def test_status_passes_through_to_the_repository():
    requests = Requests(status="the-row")
    service = TenantDeletionService(requests, Audit(), Writer())
    assert service.status(TENANT, principal_id=PRINCIPAL) == "the-row"
    assert requests.calls == [("read", TENANT, PRINCIPAL)]
