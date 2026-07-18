from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

from attune.hosted.durable import HostedTurn
from attune.hosted.google_chat_conversation_executor import (
    GoogleChatConversationExecutor,
    ConversationWork,
)
from attune.hosted.repositories import HostedJob
from attune.hosted.secret_broker_client import CalendarEventSummary, GmailThreadSummary
from attune.hosted.tenant import TenantContext

TENANT = UUID("10000000-0000-4000-8000-000000000801")
JOB = UUID("10000000-0000-4000-8000-000000000802")
CONVERSATION = UUID("10000000-0000-4000-8000-000000000803")
CONNECTOR = UUID("10000000-0000-4000-8000-000000000804")
DESTINATION = UUID("10000000-0000-4000-8000-000000000805")
EVENT = UUID("10000000-0000-4000-8000-000000000806")
INTENT = UUID("10000000-0000-4000-8000-000000000807")
NOW = datetime(2026, 7, 16, 16, tzinfo=timezone.utc)


def job(text="ignored"):
    del text
    return HostedJob(
        JOB, "channel.google_chat.converse", "leased",
        "assistant.conversation.read",
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
        assert limit == 6
        return self.turns

    def append_assistant(self, context, **kwargs):
        self.appended.append(kwargs)
        return HostedTurn(CONVERSATION, 2, "assistant", kwargs["content"], {})


class Intents:
    def __init__(self, state="requested"):
        self.calls = []
        self.state = state

    def request(self, context, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id=INTENT, state=self.state)


class Workspace:
    def __init__(self):
        self.calls = []

    def google_gmail_threads(self, intent_id, **kwargs):
        self.calls.append(("gmail", intent_id, kwargs))
        return (GmailThreadSummary("t1", "Subject", "From", "Date", "Snippet"),)

    def google_calendar_events(self, intent_id, **kwargs):
        self.calls.append(("calendar", intent_id, kwargs))
        return (CalendarEventSummary("e1", "Meeting", "start", "end", "", "confirmed"),)


class Models:
    def __init__(self, classification="general"):
        self.classification = classification
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return self.classification if kwargs["task"] == "classify" else "Here is your answer."


class Replies:
    def __init__(self):
        self.calls = []

    def deliver_google_chat_reply(self, **kwargs):
        self.calls.append(kwargs)
        return True


def execute(text, classification="general", timezone_name="UTC"):
    work, intents, workspace = Work(text), Intents(), Workspace()
    models, replies = Models(classification), Replies()
    GoogleChatConversationExecutor(
        work, intents, workspace, models, replies, now=lambda: NOW,
        timezone_name=timezone_name,
    )(TenantContext(TENANT), job())
    return work, intents, workspace, models, replies


def test_general_conversation_uses_no_workspace_authority_and_posts_canonical_reply():
    work, intents, workspace, models, replies = execute("Hello Attune")
    assert intents.calls == [] and workspace.calls == []
    assert [call["task"] for call in models.calls] == ["classify", "converse"]
    assert work.appended[0]["job_id"] == JOB
    assert replies.calls == [{"destination_id": DESTINATION, "job_id": JOB}]


def test_obvious_gmail_and_calendar_requests_override_model_under_fixed_limits():
    _, intents, workspace, models, _ = execute(
        "What unread email and calendar events do I have?", classification="general"
    )
    assert [call["capability"] for call in intents.calls] == [
        "google.gmail.threads.read", "google.calendar.events.read",
    ]
    assert workspace.calls[0][2] == {"query": "is:unread newer_than:14d", "limit": 10}
    assert workspace.calls[1][2]["limit"] == 25
    assert [call["task"] for call in models.calls] == ["converse"]


def test_deterministic_gmail_calendar_and_write_routes_skip_the_classify_call():
    _, _, _, models, _ = execute("What emails do I have?", classification="general")
    assert [call["task"] for call in models.calls] == ["converse"]

    _, _, _, models, _ = execute("What is on my calendar?", classification="general")
    assert [call["task"] for call in models.calls] == ["converse"]

    _, _, _, models, _ = execute("Give me my brief for today", classification="general")
    assert [call["task"] for call in models.calls] == ["converse"]

    _, _, _, models, _ = execute("Please send an email to the team", classification="general")
    assert models.calls == []


def test_ambiguous_request_still_calls_the_classify_model():
    _, _, _, models, _ = execute("Hello Attune", classification="general")
    assert [call["task"] for call in models.calls] == ["classify", "converse"]


def test_relative_dates_use_authoritative_local_time_and_label_live_calendar():
    _, _, _, models, _ = execute(
        "What is on my calendar tomorrow?",
        classification="calendar",
        timezone_name="America/Vancouver",
    )
    messages = models.calls[-1]["messages"]
    assert "2026-07-16T09:00:00-07:00" in messages[0]["content"]
    assert "Authoritative IANA timezone: America/Vancouver" in messages[0]["content"]
    assert "never from conversation or reference data" in messages[0]["content"]
    assert messages[-1]["content"].startswith("Live Workspace results")
    assert '"calendar_events"' in messages[-1]["content"]


def test_mutation_request_is_refused_without_workspace_or_answer_model():
    work, intents, workspace, models, replies = execute(
        "Delete that email and reschedule tomorrow's meeting", classification="gmail"
    )
    assert intents.calls == [] and workspace.calls == []
    assert models.calls == []
    assert "does not perform" in work.appended[0]["content"]
    assert replies.calls


def test_workspace_intent_is_attempt_bound_and_consumed_intent_fails_closed():
    executor = GoogleChatConversationExecutor(
        Work("Check Gmail"), Intents("consumed"), Workspace(), Models(), Replies(),
        now=lambda: NOW,
    )
    try:
        executor(TenantContext(TENANT), job())
    except RuntimeError as error:
        assert str(error) == "credential intent is unavailable"
    else:
        raise AssertionError("consumed credential intent was reused")

    first, second = Intents(), Intents()
    executor = GoogleChatConversationExecutor(
        Work("Check Gmail"), first, Workspace(), Models(), Replies(), now=lambda: NOW,
    )
    executor(TenantContext(TENANT), job())
    retry = job()
    retry = HostedJob(
        retry.id, retry.kind, retry.state, retry.capability, retry.payload,
        retry.attempts + 1, retry.available_at, retry.lease_expires_at,
    )
    executor = GoogleChatConversationExecutor(
        Work("Check Gmail"), second, Workspace(), Models(), Replies(), now=lambda: NOW,
    )
    executor(TenantContext(TENANT), retry)
    assert first.calls[0]["idempotency_key"] != second.calls[0]["idempotency_key"]
