from datetime import datetime, timezone
from uuid import UUID

import pytest

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
EVENT = UUID("10000000-0000-4000-8000-000000000906")
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
        return WebConversationWork(CONVERSATION, CONNECTOR, 1)

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
        {"conversation_id": CONVERSATION, "content": "Hello from Attune.", "job_id": JOB}
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
