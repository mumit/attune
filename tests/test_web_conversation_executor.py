import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from attune.hosted.capability_gateway import CapabilityDenied
from attune.hosted.durable import HostedTurn
from attune.hosted.repositories import HostedJob
from attune.hosted.tenant import TenantContext
from attune.hosted.web_conversation_executor import (
    CAPABILITY,
    PURPOSE,
    PostgresWebConversationWorkRepository,
    WebConversationExecutor,
    WebConversationWork,
)

TENANT = UUID("10000000-0000-4000-8000-000000000901")
JOB = UUID("10000000-0000-4000-8000-000000000902")
CONVERSATION = UUID("10000000-0000-4000-8000-000000000903")
CONNECTOR = UUID("10000000-0000-4000-8000-000000000904")
PRINCIPAL = UUID("10000000-0000-4000-8000-000000000905")
EVENT = UUID("10000000-0000-4000-8000-000000000906")
ADMISSION = UUID("10000000-0000-4000-8000-000000000907")
APPROVAL = UUID("10000000-0000-4000-8000-000000000908")
NOW = datetime(2026, 7, 17, 16, tzinfo=timezone.utc)


def job(kind: str = PURPOSE, capability: str = CAPABILITY, **payload_overrides):
    payload = {
        "schema_version": 1,
        "provider_event_id": str(EVENT),
        "conversation_id": str(CONVERSATION),
        "user_sequence": 1,
    }
    payload.update(payload_overrides)
    return HostedJob(JOB, kind, "leased", capability, payload, 1, NOW, NOW)


class Work:
    def __init__(self, text):
        self.turns = [HostedTurn(CONVERSATION, 1, "user", text, {})]
        self.appended = []

    def resolve(self, context, value):
        assert context == TenantContext(TENANT) and value.id == JOB
        return WebConversationWork(CONVERSATION, PRINCIPAL, CONNECTOR, 1)

    def recent(self, context, conversation_id, *, limit):
        return self.turns

    def append_assistant(self, context, **kwargs):
        self.appended.append(kwargs)
        return HostedTurn(CONVERSATION, 2, "assistant", kwargs["content"], {})


class Models:
    def __init__(self, classified="general", answer="Hello from Attune."):
        self.classified = classified
        self.answer = answer
        self.calls = []

    def complete(self, *, task, messages):
        self.calls.append(task)
        return self.classified if task == "classify" else self.answer


def test_web_conversation_appends_the_assistant_turn_and_calls_no_reply_broker():
    work, models = Work("hi there"), Models()
    WebConversationExecutor(work, None, None, models, now=lambda: NOW)(
        TenantContext(TENANT), job()
    )
    assert work.appended == [
        {
            "conversation_id": CONVERSATION,
            "content": "Hello from Attune.",
            "job_id": JOB,
            "extra_provenance": {},
        }
    ]
    # "hi there" is ambiguous for the deterministic keyword router, so the
    # model classify call still runs before the converse call.
    assert models.calls == ["classify", "converse"]


def test_web_conversation_mutation_request_is_refused_without_answer_model():
    work, models = Work("please send an email to the team"), Models(classified="general")
    WebConversationExecutor(work, None, None, models, now=lambda: NOW)(
        TenantContext(TENANT), job()
    )
    assert "does not perform email or calendar changes" in work.appended[0]["content"]
    # The write keyword is a clearly-deterministic route, so the model is
    # never invoked at all -- not even to classify.
    assert models.calls == []


def test_web_conversation_refuses_a_mismatched_job_kind():
    mismatched = job(kind="channel.slack.converse")
    repository = PostgresWebConversationWorkRepository(lambda: None)
    with pytest.raises(ValueError, match="fixed route"):
        repository.resolve(TenantContext(TENANT), mismatched)


def test_web_conversation_refuses_a_payload_with_a_destination_id():
    mismatched = job(destination_id=str(CONNECTOR))
    repository = PostgresWebConversationWorkRepository(lambda: None)
    with pytest.raises(ValueError, match="contract"):
        repository.resolve(TenantContext(TENANT), mismatched)


def test_web_conversation_refuses_a_non_uuid_conversation_reference():
    mismatched = job(conversation_id="not-a-uuid")
    repository = PostgresWebConversationWorkRepository(lambda: None)
    with pytest.raises(ValueError, match="reference"):
        repository.resolve(TenantContext(TENANT), mismatched)


# -- Hosted draft-and-approve capability (docs/capability-gateway.md) -------
# Dormant unless both capability_gateway and capability_admissions are
# injected; worker_app.py only ever does so under
# ATTUNE_ENABLE_HOSTED_DRAFT_CAPABILITY, and only for this (web) executor.


class TwoTurnWork:
    """Lets a test hand-construct the exact prior-turn provenance a draft
    decision needs, mirroring how a real conversation accumulates turns
    across two separate worker jobs."""

    def __init__(self, turns):
        self.turns = turns
        self.appended = []

    def resolve(self, context, value):
        return WebConversationWork(CONVERSATION, PRINCIPAL, CONNECTOR, self.turns[-1].sequence)

    def recent(self, context, conversation_id, *, limit):
        return self.turns[-limit:]

    def append_assistant(self, context, **kwargs):
        self.appended.append(kwargs)
        return HostedTurn(
            CONVERSATION, self.turns[-1].sequence + 1, "assistant",
            kwargs["content"], kwargs.get("extra_provenance") or {},
        )


class FakeGateway:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def authorize(self, context, *, principal_id, proposal):
        self.calls.append((context, principal_id, proposal))
        if self.error is not None:
            raise self.error
        return self.result


class FakeAdmissions:
    def __init__(self, decide_status="consumed"):
        self.record_calls = []
        self.decide_calls = []
        self.decide_status = decide_status

    def record(self, context, *, authorized, destination_hash):
        self.record_calls.append((context, authorized, destination_hash))
        return SimpleNamespace(admission_id=ADMISSION, approval_id=APPROVAL)

    def decide(self, context, *, approval_id, principal_id, decision):
        self.decide_calls.append((context, approval_id, principal_id, decision))
        return self.decide_status


def test_gate_off_draft_reply_falls_through_to_the_byte_identical_refusal():
    """Pin: with no capability gateway/admissions injected (the gate-off,
    and every non-web-surface, case), "draft reply ...: ..." is ordinary
    write-shaped text -- the exact pre-stage-3 mutation refusal, because it
    still contains the deterministic _WRITE keyword "reply"."""
    work = Work("draft reply thread_1: catch you tomorrow")
    models = Models(classified="general")
    WebConversationExecutor(work, None, None, models, now=lambda: NOW)(
        TenantContext(TENANT), job()
    )
    assert "does not perform email or calendar changes" in work.appended[0]["content"]
    assert models.calls == []


def test_draft_gateway_denied_gives_an_honest_policy_message_and_never_records():
    work = Work("draft reply thread_1: see you then")
    gateway = FakeGateway(error=CapabilityDenied("authority_unavailable"))
    admissions = FakeAdmissions()
    WebConversationExecutor(
        work, None, None, Models(), now=lambda: NOW,
        capability_gateway=gateway, capability_admissions=admissions,
    )(TenantContext(TENANT), job())
    assert "authorized by your current policy" in work.appended[0]["content"]
    assert admissions.record_calls == []


def test_draft_admitted_records_admission_and_pending_approval_but_never_dispatches():
    """The admission-persists-but-never-dispatches-without-approval pin:
    a successful gateway admission records the admission/approval and asks
    for approval, but the executor never calls decide() -- and therefore
    never dispatches -- until a later 'approve draft'/'reject draft' turn."""
    work = Work("draft reply thread_1: see you then")
    authorized = object()
    gateway = FakeGateway(result=authorized)
    admissions = FakeAdmissions()
    WebConversationExecutor(
        work, None, None, Models(), now=lambda: NOW,
        capability_gateway=gateway, capability_admissions=admissions,
    )(TenantContext(TENANT), job())
    assert admissions.record_calls == [
        (TenantContext(TENANT), authorized, hashlib.sha256(b"thread_1").digest())
    ]
    assert admissions.decide_calls == []
    turn = work.appended[0]
    assert "approve draft" in turn["content"] and "reject draft" in turn["content"]
    assert turn["extra_provenance"] == {"pending_draft_approval_id": str(APPROVAL)}


def test_draft_approve_with_pending_admission_claims_and_dispatches():
    turns = [
        HostedTurn(
            CONVERSATION, 1, "assistant", "I've prepared a draft...",
            {"pending_draft_approval_id": str(APPROVAL)},
        ),
        HostedTurn(CONVERSATION, 2, "user", "approve draft", {}),
    ]
    work = TwoTurnWork(turns)
    admissions = FakeAdmissions(decide_status="consumed")
    WebConversationExecutor(
        work, None, None, Models(), now=lambda: NOW,
        capability_gateway=FakeGateway(), capability_admissions=admissions,
    )(TenantContext(TENANT), job(user_sequence=2))
    assert admissions.decide_calls == [
        (TenantContext(TENANT), APPROVAL, PRINCIPAL, "approved")
    ]
    assert "creating that draft" in work.appended[0]["content"]


def test_draft_reject_never_dispatches():
    turns = [
        HostedTurn(
            CONVERSATION, 1, "assistant", "I've prepared a draft...",
            {"pending_draft_approval_id": str(APPROVAL)},
        ),
        HostedTurn(CONVERSATION, 2, "user", "reject draft", {}),
    ]
    work = TwoTurnWork(turns)
    admissions = FakeAdmissions(decide_status="rejected")
    WebConversationExecutor(
        work, None, None, Models(), now=lambda: NOW,
        capability_gateway=FakeGateway(), capability_admissions=admissions,
    )(TenantContext(TENANT), job(user_sequence=2))
    assert admissions.decide_calls == [
        (TenantContext(TENANT), APPROVAL, PRINCIPAL, "rejected")
    ]
    assert "discarded" in work.appended[0]["content"]


def test_draft_approve_without_pending_provenance_is_honest_and_never_claims():
    turns = [HostedTurn(CONVERSATION, 1, "user", "approve draft", {})]
    work = TwoTurnWork(turns)
    admissions = FakeAdmissions()
    WebConversationExecutor(
        work, None, None, Models(), now=lambda: NOW,
        capability_gateway=FakeGateway(), capability_admissions=admissions,
    )(TenantContext(TENANT), job(user_sequence=1))
    assert "no pending draft" in work.appended[0]["content"].lower()
    assert admissions.decide_calls == []


def test_draft_double_approve_calls_decide_again_and_stays_idempotent_looking():
    """decide()'s own one-use idempotency is exercised in
    test_capability_admission.py and the gated Postgres suite; this pins
    that the executor itself calls decide() again on a second identical
    "approve draft" turn (never caching a local "already handled" flag) and
    that a stable "consumed" outcome always produces the same honest
    confirmation."""
    admissions = FakeAdmissions(decide_status="consumed")
    for sequence in (2, 4):
        turns = [
            HostedTurn(
                CONVERSATION, sequence - 1, "assistant", "I've prepared a draft...",
                {"pending_draft_approval_id": str(APPROVAL)},
            ),
            HostedTurn(CONVERSATION, sequence, "user", "approve draft", {}),
        ]
        work = TwoTurnWork(turns)
        WebConversationExecutor(
            work, None, None, Models(), now=lambda: NOW,
            capability_gateway=FakeGateway(), capability_admissions=admissions,
        )(TenantContext(TENANT), job(user_sequence=sequence))
        assert "creating that draft" in work.appended[0]["content"]
    assert admissions.decide_calls == [
        (TenantContext(TENANT), APPROVAL, PRINCIPAL, "approved"),
        (TenantContext(TENANT), APPROVAL, PRINCIPAL, "approved"),
    ]


def test_draft_decide_failure_is_reported_honestly_not_as_success():
    class RaisingAdmissions(FakeAdmissions):
        def decide(self, *args, **kwargs):
            super().decide(*args, **kwargs)
            raise RuntimeError("capability dispatch was refused")

    turns = [
        HostedTurn(
            CONVERSATION, 1, "assistant", "I've prepared a draft...",
            {"pending_draft_approval_id": str(APPROVAL)},
        ),
        HostedTurn(CONVERSATION, 2, "user", "approve draft", {}),
    ]
    work = TwoTurnWork(turns)
    WebConversationExecutor(
        work, None, None, Models(), now=lambda: NOW,
        capability_gateway=FakeGateway(), capability_admissions=RaisingAdmissions(),
    )(TenantContext(TENANT), job(user_sequence=2))
    assert "couldn't queue" in work.appended[0]["content"]
