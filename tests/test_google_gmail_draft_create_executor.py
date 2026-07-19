from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from attune.hosted.google_gmail_draft_create_executor import (
    CAPABILITY,
    GoogleGmailDraftCreateExecutor,
)
from attune.hosted.repositories import HostedJob
from attune.hosted.tenant import TenantContext
from attune.hosted.vault import CredentialIntent

TENANT = TenantContext(UUID("10000000-0000-4000-8000-000000000731"))
JOB = UUID("10000000-0000-4000-8000-000000000732")
CONNECTOR = UUID("aaaaaaaa-0000-4000-8000-000000000733")
ADMISSION = UUID("10000000-0000-4000-8000-000000000734")
INTENT = UUID("10000000-0000-4000-8000-000000000735")
NOW = datetime(2026, 7, 19, 16, 0, tzinfo=timezone.utc)


def job(payload=None, *, kind=CAPABILITY, capability=CAPABILITY):
    return HostedJob(
        JOB,
        kind,
        "leased",
        capability,
        payload
        or {
            "schema_version": 1,
            "admission_id": str(ADMISSION),
            "connector_id": str(CONNECTOR),
            "thread_ref": "thread_1",
            "body": "Hello there",
        },
        1,
        NOW,
        NOW,
    )


class Intents:
    def __init__(self, state="requested"):
        self.state = state
        self.calls = []

    def request(self, context, **kwargs):
        self.calls.append((context, kwargs))
        return CredentialIntent(INTENT, CONNECTOR, "worker", "use", CAPABILITY, self.state)


class Broker:
    def __init__(self):
        self.calls = []

    def google_gmail_draft_create(self, intent_id, *, thread_ref, body):
        self.calls.append((intent_id, thread_ref, body))
        return "draft_1"


def test_executor_creates_one_fixed_short_lived_intent_and_calls_broker():
    intents = Intents()
    broker = Broker()
    GoogleGmailDraftCreateExecutor(intents, broker, now=lambda: NOW)(TENANT, job())
    assert broker.calls == [(INTENT, "thread_1", "Hello there")]
    context, request = intents.calls[0]
    assert context == TENANT
    assert request["connector_id"] == CONNECTOR
    assert request["operation"] == "use"
    assert request["capability"] == CAPABILITY
    assert len(request["idempotency_key"]) == 32
    assert (request["expires_at"] - NOW).total_seconds() == 120


def test_executor_treats_consumed_intent_as_idempotent_success():
    intents = Intents("consumed")
    broker = Broker()
    GoogleGmailDraftCreateExecutor(intents, broker, now=lambda: NOW)(TENANT, job())
    assert broker.calls == []


@pytest.mark.parametrize(
    "candidate",
    [
        job(kind="other"),
        job(capability="google.gmail.threads.read"),
        job({"admission_id": str(ADMISSION), "connector_id": str(CONNECTOR),
             "thread_ref": "thread_1", "body": "Hi"}),  # missing schema_version
        job({"schema_version": 1, "admission_id": str(ADMISSION),
             "connector_id": "not-a-uuid", "thread_ref": "thread_1", "body": "Hi"}),
        job({"schema_version": 1, "admission_id": str(ADMISSION),
             "connector_id": str(CONNECTOR), "thread_ref": "../etc", "body": "Hi"}),
        job({"schema_version": 1, "admission_id": str(ADMISSION),
             "connector_id": str(CONNECTOR), "thread_ref": "thread_1", "body": ""}),
        job({"schema_version": 1, "admission_id": str(ADMISSION),
             "connector_id": str(CONNECTOR), "thread_ref": "thread_1",
             "body": "x" * 10_001}),
        job({"schema_version": 1, "admission_id": str(ADMISSION),
             "connector_id": str(CONNECTOR), "thread_ref": "thread_1", "body": "Hi",
             "extra": "field"}),
    ],
)
def test_executor_rejects_authority_outside_fixed_contract(candidate):
    intents = Intents()
    broker = Broker()
    with pytest.raises(ValueError):
        GoogleGmailDraftCreateExecutor(intents, broker, now=lambda: NOW)(TENANT, candidate)
    assert intents.calls == []
    assert broker.calls == []


def test_executor_rejects_ambiguous_intent_state():
    with pytest.raises(RuntimeError):
        GoogleGmailDraftCreateExecutor(Intents("leased"), Broker(), now=lambda: NOW)(
            TENANT, job()
        )
