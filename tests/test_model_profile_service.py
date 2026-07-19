from __future__ import annotations

from uuid import UUID

import pytest

from attune.hosted.audit import HostedAuditIntent
from attune.hosted.model_profile import TenantModelProfile
from attune.hosted.model_profile_service import HostedModelProfileService
from attune.hosted.tenant import TenantContext

TENANT = TenantContext(UUID("10000000-0000-4000-8000-000000000001"))
PRINCIPAL = UUID("10000000-0000-4000-8000-000000000002")
SESSION = UUID("10000000-0000-4000-8000-000000000003")


class Profiles:
    def __init__(self, error=None):
        self.error = error
        self.calls = []
        self.read_calls = 0

    def read(self, context):
        self.read_calls += 1
        return None

    def set(self, context, **kwargs):
        self.calls.append((context, kwargs))
        if self.error:
            raise self.error
        return TenantModelProfile(1, kwargs["profile"], 1)


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


def test_model_profile_is_mandatorily_audited_allowed_then_observed():
    profiles, audit, writer = Profiles(), Audit(), Writer((True, True))
    service = HostedModelProfileService(profiles, audit, writer)
    result = service.configure(
        TENANT, principal_id=PRINCIPAL, session_id=SESSION, profile="premium",
    )
    assert result.profile == "premium"
    assert [call[1]["outcome"] for call in audit.calls] == ["allowed", "observed"]
    assert profiles.calls[0][1]["profile"] == "premium"
    # Content-free: metadata carries only schema_version, never the profile
    # name or any other value.
    assert audit.calls[0][1]["metadata"] == {"schema_version": 1}
    assert audit.calls[0][1]["action"] == "hosted.model_profile.configure"


def test_pre_effect_audit_failure_prevents_configuration():
    profiles = Profiles()
    with pytest.raises(RuntimeError, match="pre-effect audit"):
        HostedModelProfileService(profiles, Audit(), Writer((False,))).configure(
            TENANT, principal_id=PRINCIPAL, session_id=SESSION, profile="standard",
        )
    assert profiles.calls == []


def test_database_failure_is_followed_by_a_failed_audit():
    audit = Audit()
    with pytest.raises(RuntimeError, match="session"):
        HostedModelProfileService(
            Profiles(RuntimeError("session expired")), audit, Writer()
        ).configure(
            TENANT, principal_id=PRINCIPAL, session_id=SESSION, profile="standard",
        )
    assert [call[1]["outcome"] for call in audit.calls] == ["allowed", "failed"]


def test_outcome_audit_failure_still_raises_after_a_successful_write():
    profiles = Profiles()
    with pytest.raises(RuntimeError, match="outcome audit"):
        HostedModelProfileService(
            profiles, Audit(), Writer((True, False))
        ).configure(
            TENANT, principal_id=PRINCIPAL, session_id=SESSION, profile="standard",
        )
    assert len(profiles.calls) == 1


def test_invalid_profile_type_is_rejected_before_any_audit():
    with pytest.raises(ValueError):
        HostedModelProfileService(Profiles(), Audit(), Writer()).configure(
            TENANT, principal_id=PRINCIPAL, session_id=SESSION, profile=123,
        )


def test_read_passes_through_to_the_repository():
    profiles = Profiles()
    service = HostedModelProfileService(profiles, Audit(), Writer())
    assert service.read(TENANT) is None
    assert profiles.read_calls == 1
