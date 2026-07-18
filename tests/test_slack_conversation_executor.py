from datetime import datetime, timezone
from uuid import UUID

import pytest

from attune.hosted.durable import HostedTurn
from attune.hosted.google_chat_conversation_executor import ConversationWork
from attune.hosted.repositories import HostedJob
from attune.hosted.slack_conversation_executor import (
    PURPOSE,
    SlackConversationExecutor,
)
from attune.hosted.tenant import TenantContext

TENANT = UUID("10000000-0000-4000-8000-000000000801")
JOB = UUID("10000000-0000-4000-8000-000000000802")
CONVERSATION = UUID("10000000-0000-4000-8000-000000000803")
CONNECTOR = UUID("10000000-0000-4000-8000-000000000804")
DESTINATION = UUID("10000000-0000-4000-8000-000000000805")
EVENT = UUID("10000000-0000-4000-8000-000000000806")
INTENT = UUID("10000000-0000-4000-8000-000000000807")
NOW = datetime(2026, 7, 16, 16, tzinfo=timezone.utc)


def job():
    return HostedJob(
        JOB, PURPOSE, "leased", "assistant.conversation.read",
        {"schema_version": 1, "provider_event_id": str(EVENT),
         "conversation_id": str(CONVERSATION), "user_sequence": 1,
         "destination_id": str(DESTINATION)},
        1, NOW, NOW,
    )


class Work:
    def __init__(self, text):
        self.turns = [HostedTurn(CONVERSATION, 1, "user", text, {})]
        self.appended = []

    def resolve(self, context, value):
        assert context == TenantContext(TENANT) and value.id == JOB
        return ConversationWork(CONVERSATION, CONNECTOR, DESTINATION, 1)

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


class Replies:
    def __init__(self, result=True):
        self.result = result
        self.slack_calls = []
        self.google_calls = []

    def deliver_slack_reply(self, **kwargs):
        self.slack_calls.append(kwargs)
        return self.result

    def deliver_google_chat_reply(self, **kwargs):
        self.google_calls.append(kwargs)
        return self.result


def test_slack_conversation_delivers_through_the_slack_reply_route_only():
    work, replies, models = Work("hi there"), Replies(), Models()
    SlackConversationExecutor(
        work, None, None, models, replies, now=lambda: NOW
    )(TenantContext(TENANT), job())
    assert replies.slack_calls == [{"destination_id": DESTINATION, "job_id": JOB}]
    assert replies.google_calls == []
    assert work.appended[0]["content"] == "Hello from Attune."
    # "hi there" is ambiguous for the deterministic keyword router, so the
    # model classify call still runs before the converse call.
    assert models.calls == ["classify", "converse"]


def test_slack_conversation_refuses_google_chat_job_kind():
    mismatched = HostedJob(
        JOB, "channel.google_chat.converse", "leased",
        "assistant.conversation.read",
        {"schema_version": 1, "provider_event_id": str(EVENT),
         "conversation_id": str(CONVERSATION), "user_sequence": 1,
         "destination_id": str(DESTINATION)},
        1, NOW, NOW,
    )
    from attune.hosted.slack_conversation_executor import (
        PostgresSlackConversationWorkRepository,
    )

    repository = PostgresSlackConversationWorkRepository(lambda: None)
    with pytest.raises(ValueError, match="fixed route"):
        repository.resolve(TenantContext(TENANT), mismatched)


def test_slack_conversation_undelivered_reply_fails_the_job():
    work = Work("hi there")
    with pytest.raises(RuntimeError, match="reply"):
        SlackConversationExecutor(
            work, None, None, Models(), Replies(result=False), now=lambda: NOW
        )(TenantContext(TENANT), job())


def test_slack_mutation_request_is_refused_without_answer_model():
    work, replies, models = Work("please send an email to the team"), Replies(), Models(
        classified="general"
    )
    SlackConversationExecutor(
        work, None, None, models, replies, now=lambda: NOW
    )(TenantContext(TENANT), job())
    assert "does not perform email or calendar changes" in work.appended[0]["content"]
    assert replies.slack_calls
    # The write keyword is a clearly-deterministic route, so the model is
    # never invoked at all -- not even to classify.
    assert models.calls == []
