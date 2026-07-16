from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from attune.hosted.google_workspace_verification_executor import (
    CALENDAR_CAPABILITY,
    CAPABILITY,
    GMAIL_CAPABILITY,
    GoogleWorkspaceVerificationExecutor,
)
from attune.hosted.repositories import HostedJob
from attune.hosted.secret_broker_client import GmailProfile
from attune.hosted.tenant import TenantContext
from attune.hosted.vault import CredentialIntent

TENANT = TenantContext(UUID("10000000-0000-4000-8000-000000000541"))
JOB = UUID("10000000-0000-4000-8000-000000000542")
CONNECTOR = UUID("aaaaaaaa-0000-4000-8000-000000000543")
NOW = datetime(2026, 7, 15, 16, 0, tzinfo=timezone.utc)


def job(payload=None, *, kind=CAPABILITY, capability=CAPABILITY):
    return HostedJob(
        JOB,
        kind,
        "leased",
        capability,
        payload or {"connector_id": str(CONNECTOR)},
        1,
        NOW,
        NOW,
    )


class Intents:
    def __init__(self, states=None):
        self.states = iter(states or ["requested", "requested"])
        self.calls = []

    def request(self, context, **kwargs):
        self.calls.append((context, kwargs))
        index = len(self.calls)
        return CredentialIntent(
            UUID(f"10000000-0000-4000-8000-{index:012d}"),
            CONNECTOR,
            "worker",
            "use",
            kwargs["capability"],
            next(self.states),
        )


class Broker:
    def __init__(self):
        self.calls = []

    def google_gmail_profile(self, intent_id):
        self.calls.append((GMAIL_CAPABILITY, intent_id))
        return GmailProfile("123", 4, 3)

    def google_calendar_primary(self, intent_id):
        self.calls.append((CALENDAR_CAPABILITY, intent_id))


def test_executor_requires_two_separate_short_lived_capabilities():
    intents, broker = Intents(), Broker()
    GoogleWorkspaceVerificationExecutor(intents, broker, now=lambda: NOW)(TENANT, job())
    assert [call[1]["capability"] for call in intents.calls] == [
        GMAIL_CAPABILITY,
        CALENDAR_CAPABILITY,
    ]
    assert all(call[1]["operation"] == "use" for call in intents.calls)
    assert all(call[1]["connector_id"] == CONNECTOR for call in intents.calls)
    assert all(
        (call[1]["expires_at"] - NOW).total_seconds() == 120
        for call in intents.calls
    )
    assert [capability for capability, _ in broker.calls] == [
        GMAIL_CAPABILITY,
        CALENDAR_CAPABILITY,
    ]


def test_executor_resumes_after_consumed_gmail_intent():
    intents, broker = Intents(["consumed", "requested"]), Broker()
    GoogleWorkspaceVerificationExecutor(intents, broker, now=lambda: NOW)(TENANT, job())
    assert [capability for capability, _ in broker.calls] == [CALENDAR_CAPABILITY]


@pytest.mark.parametrize(
    "candidate",
    [
        job({"connector_id": str(CONNECTOR), "url": "https://evil.example"}),
        job({"connector_id": "not-a-uuid"}),
        job(kind="other"),
        job(capability="google.calendar.events.read"),
    ],
)
def test_executor_rejects_authority_outside_fixed_contract(candidate):
    intents, broker = Intents(), Broker()
    with pytest.raises(ValueError):
        GoogleWorkspaceVerificationExecutor(intents, broker, now=lambda: NOW)(
            TENANT, candidate
        )
    assert intents.calls == [] and broker.calls == []


def test_executor_fails_closed_on_ambiguous_intent_state():
    with pytest.raises(RuntimeError):
        GoogleWorkspaceVerificationExecutor(
            Intents(["leased"]), Broker(), now=lambda: NOW
        )(TENANT, job())
