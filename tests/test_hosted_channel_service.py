from __future__ import annotations

from uuid import UUID

import pytest

from attune.hosted.audit import HostedAuditIntent
from attune.hosted.hosted_channel_service import HostedChannelService
from attune.hosted.hosted_channels import HostedChannelPreferences, normalize_channels
from attune.hosted.tenant import TenantContext

TENANT = TenantContext(UUID("10000000-0000-4000-8000-000000000001"))
PRINCIPAL = UUID("10000000-0000-4000-8000-000000000002")
SESSION = UUID("10000000-0000-4000-8000-000000000003")


class Channels:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def read(self, context, *, principal_id):
        return None

    def configure(self, context, **kwargs):
        self.calls.append((context, kwargs))
        if self.error:
            raise self.error
        return HostedChannelPreferences(
            1,
            1,
            tuple(kwargs["interaction_channels"]),
            tuple(kwargs["brief_channels"]),
            2,
            "authorized",
        )


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


def test_channel_preferences_are_canonical_and_mandatorily_audited():
    channels, audit, writer = Channels(), Audit(), Writer((True, True, True, True))
    service = HostedChannelService(channels, audit, writer)
    result = service.configure(
        TENANT,
        principal_id=PRINCIPAL,
        session_id=SESSION,
        interaction_channels=["slack", "google_chat"],
        brief_channels=["slack"],
    )
    assert result.interaction_channels == ("google_chat", "slack")
    assert [call[1]["outcome"] for call in audit.calls] == ["allowed", "observed"]
    assert channels.calls[0][1]["interaction_channels"] == ("google_chat", "slack")
    service.configure(
        TENANT,
        principal_id=PRINCIPAL,
        session_id=SESSION,
        interaction_channels=["slack", "google_chat"],
        brief_channels=["slack"],
    )
    assert audit.calls[0][1]["idempotency_key"] != audit.calls[2][1]["idempotency_key"]


@pytest.mark.parametrize(
    "interaction,brief",
    [([], []), (["email"], []), (["slack", "slack"], [])],
)
def test_invalid_preferences_are_rejected_before_audit(interaction, brief):
    channels, audit = Channels(), Audit()
    with pytest.raises(ValueError):
        HostedChannelService(channels, audit, Writer()).configure(
            TENANT,
            principal_id=PRINCIPAL,
            session_id=SESSION,
            interaction_channels=interaction,
            brief_channels=brief,
        )
    assert audit.calls == []
    assert channels.calls == []


def test_pre_effect_audit_failure_prevents_configuration():
    channels = Channels()
    with pytest.raises(RuntimeError, match="pre-effect audit"):
        HostedChannelService(channels, Audit(), Writer((False,))).configure(
            TENANT,
            principal_id=PRINCIPAL,
            session_id=SESSION,
            interaction_channels=["google_chat"],
            brief_channels=[],
        )
    assert channels.calls == []


def test_database_failure_is_followed_by_failed_audit():
    audit = Audit()
    with pytest.raises(RuntimeError, match="recent session"):
        HostedChannelService(
            Channels(RuntimeError("recent session expired")), audit, Writer()
        ).configure(
            TENANT,
            principal_id=PRINCIPAL,
            session_id=SESSION,
            interaction_channels=["slack"],
            brief_channels=[],
        )
    assert [call[1]["outcome"] for call in audit.calls] == ["allowed", "failed"]


def test_channel_normalization_requires_a_bounded_list():
    with pytest.raises(ValueError):
        normalize_channels("channels", "slack")
