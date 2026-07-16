from __future__ import annotations

from uuid import UUID

import pytest

from attune.hosted.audit import HostedAuditIntent
from attune.hosted.hosted_policy import HostedPolicyActivation
from attune.hosted.hosted_policy_service import HostedPolicyService
from attune.hosted.tenant import TenantContext

TENANT = TenantContext(UUID("10000000-0000-4000-8000-000000000001"))
PRINCIPAL = UUID("10000000-0000-4000-8000-000000000002")
SESSION = UUID("10000000-0000-4000-8000-000000000003")


class Policies:
    def __init__(self, status="validated", error=None):
        self.status = status
        self.error = error
        self.calls = []

    def activate_read_only(self, context, *, principal_id, session_id):
        self.calls.append((context, principal_id, session_id))
        if self.error:
            raise self.error
        return HostedPolicyActivation(1, 2, self.status)


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


def test_policy_change_requires_pre_effect_and_observed_audit():
    policies, audit, writer = Policies(), Audit(), Writer()
    result = HostedPolicyService(policies, audit, writer).activate_read_only(
        TENANT, principal_id=PRINCIPAL, session_id=SESSION
    )
    assert result.status == "validated"
    assert policies.calls == [(TENANT, PRINCIPAL, SESSION)]
    assert [call[1]["outcome"] for call in audit.calls] == ["allowed", "observed"]
    assert writer.calls == [UUID(int=1), UUID(int=2)]
    assert all(call[1]["actor_ref_hash"] for call in audit.calls)
    assert all(call[1]["target_ref_hash"] for call in audit.calls)
    assert audit.calls[0][1]["idempotency_key"] != audit.calls[1][1]["idempotency_key"]


def test_pre_effect_audit_failure_prevents_policy_mutation():
    policies, audit, writer = Policies(), Audit(), Writer((False,))
    with pytest.raises(RuntimeError, match="pre-effect audit"):
        HostedPolicyService(policies, audit, writer).activate_read_only(
            TENANT, principal_id=PRINCIPAL, session_id=SESSION
        )
    assert policies.calls == []


def test_external_modification_records_failed_outcome():
    policies, audit, writer = Policies("externally_modified"), Audit(), Writer()
    result = HostedPolicyService(policies, audit, writer).activate_read_only(
        TENANT, principal_id=PRINCIPAL, session_id=SESSION
    )
    assert result.status == "externally_modified"
    assert [call[1]["outcome"] for call in audit.calls] == ["allowed", "failed"]


def test_database_refusal_is_followed_by_failed_audit():
    policies = Policies(error=RuntimeError("recent session expired"))
    audit, writer = Audit(), Writer()
    with pytest.raises(RuntimeError, match="recent session"):
        HostedPolicyService(policies, audit, writer).activate_read_only(
            TENANT, principal_id=PRINCIPAL, session_id=SESSION
        )
    assert [call[1]["outcome"] for call in audit.calls] == ["allowed", "failed"]


def test_outcome_audit_failure_is_visible_after_idempotent_policy_effect():
    policies, audit, writer = Policies(), Audit(), Writer((True, False))
    with pytest.raises(RuntimeError, match="outcome audit"):
        HostedPolicyService(policies, audit, writer).activate_read_only(
            TENANT, principal_id=PRINCIPAL, session_id=SESSION
        )
    assert policies.calls == [(TENANT, PRINCIPAL, SESSION)]
