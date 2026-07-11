"""Tests for runtime.py — the always-on entrypoint's wiring logic.

Only the testable half is exercised here (per the module's own docstring):
``build_runtime``'s override-resolution and ``process_*``/``renew_*``'s
routing. The live loops (``run``, ``run_*_pubsub_loop``) need a real GCP
project and Slack workspace and are excluded by design, same as
``SlackChannel.start()``.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from types import ModuleType
from unittest.mock import patch

import pytest

from aidedecamp.app import AppContext
from aidedecamp.config import Settings
from aidedecamp.connectors.base import Provenance
from aidedecamp.ingestion.state import JsonChatSubscriptionState, JsonGmailWatchState
from aidedecamp.runtime import Runtime, build_runtime


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _FakeThread:
    def __init__(self, thread_id="t1", subject="Hi", from_addr="a@b.com", body="body"):
        self.thread_id = thread_id
        self.subject = subject
        self.from_addr = from_addr
        self.body = body
        self.snippet = body[:20]
        self.labels: list[str] = []
        self.received_at = None
        self.provenance = Provenance.FETCHED


class _FakeConnector:
    def __init__(self, threads=None, events=None):
        self._threads = threads if threads is not None else {"t1": _FakeThread()}
        self._events = events or []

    def get_thread(self, thread_id):
        return self._threads[thread_id]

    def list_threads(self, *a, **kw):
        return list(self._threads.values())

    def list_events(self, *a, **kw):
        return self._events

    def create_draft(self, *a, **kw): ...


class _FakeGmailService:
    """One page of history with a single messagesAdded threadId."""

    def __init__(self, thread_ids=("t1",)):
        self._thread_ids = thread_ids

    def users(self):
        tids = self._thread_ids

        class _History:
            def list(self, **kwargs):
                class _Req:
                    def execute(self_):
                        return {
                            "history": [
                                {
                                    "messagesAdded": [
                                        {"message": {"threadId": tid, "id": f"m_{tid}"}}
                                        for tid in tids
                                    ]
                                }
                            ]
                        }
                return _Req()

        class _Users:
            def history(self):
                return _History()

            def watch(self, userId, body):
                class _Req:
                    def execute(self_):
                        return {"historyId": "999", "expiration": "99999999999999"}
                return _Req()

        return _Users()


class _FakeWatchState:
    def __init__(self, email="me", history_id="100"):
        self._data = {email: {"history_id": history_id, "expiration": datetime.now(timezone.utc)}}

    def get(self, email):
        return self._data.get(email)

    def put(self, email, *, history_id, expiration):
        self._data[email] = {"history_id": history_id, "expiration": expiration}


class _FakeChatState:
    def __init__(self):
        self._data: dict = {}

    def get(self, space):
        return self._data.get(space)

    def put(self, space, *, subscription_name, expiration):
        self._data[space] = {"subscription_name": subscription_name, "expiration": expiration}


class _FakeGraph:
    def __init__(self, proposed="draft text", memories=None, audit_events=None):
        self._proposed = proposed
        self._memories = memories or []
        self._audit_events = audit_events or []
        self.calls: list[dict] = []

    def invoke(self, state, config):
        self.calls.append({"state": state, "config": config})
        return {
            "proposed_draft": self._proposed,
            "retrieved_memories": self._memories,
            "audit_events": self._audit_events,
        }


class _FakeMemoryStore:
    def __init__(self, results=None):
        self._results = results or []

    def search(self, query, *, user_id, limit=5):
        return self._results

    def add(self, *a, **kw): pass


class _FakeClient:
    def __init__(self, reply="assistant reply"):
        self._reply = reply
        self.calls: list = []

    def chat_completions_create(self, **kwargs):
        self.calls.append(kwargs)
        class _Choice:
            class message:
                content = None
        _Choice.message.content = self._reply
        class _Resp:
            choices = [_Choice]
        return _Resp()


class _FakeAuditLog:
    def __init__(self):
        self.records: list[dict] = []

    def record(self, **kwargs):
        self.records.append(kwargs)

    def query(self, **kwargs):
        return []


class _FakeSlackChannel:
    def __init__(self):
        self.approvals: list[dict] = []

    def post_approval(self, say, *, thread_id, domain, proposed_draft, rationale=None):
        self.approvals.append(
            {"say": say, "thread_id": thread_id, "domain": domain,
             "proposed_draft": proposed_draft, "rationale": rationale}
        )


class _FakeGChatChannel:
    def __init__(self):
        self.approvals: list[dict] = []
        self.texts: list[tuple] = []

    def post_approval(self, space, *, thread_id, domain, proposed_draft, rationale=None):
        self.approvals.append(
            {"space": space, "thread_id": thread_id, "domain": domain,
             "proposed_draft": proposed_draft, "rationale": rationale}
        )

    def post_text(self, space, text):
        self.texts.append((space, text))


def _settings(**overrides):
    base = {
        "ADC_DEPLOYMENT": "personal",
        "ADC_CONNECTOR_MODE": "mcp",
        "ADC_MEM0_URL": "",
        "ADC_AUDIT_LOG_PATH": "",
    }
    base.update(overrides)
    return Settings.from_env(base)


def _app_ctx(graph=None, client=None, store=None, audit_log=None):
    return AppContext(
        graph=graph or _FakeGraph(),
        client=client or _FakeClient(),
        store=store or _FakeMemoryStore(),
        settings=_settings(),
        audit_log=audit_log or _FakeAuditLog(),
    )


# ---------------------------------------------------------------------------
# build_runtime — override resolution
# ---------------------------------------------------------------------------


def test_build_runtime_uses_all_overrides():
    app = _app_ctx()
    connector = _FakeConnector()
    gmail_service = _FakeGmailService()
    watch_state = _FakeWatchState()
    chat_state = _FakeChatState()
    slack = _FakeSlackChannel()
    gchat = _FakeGChatChannel()

    runtime = build_runtime(
        _settings(),
        app=app,
        connector=connector,
        gmail_service=gmail_service,
        watch_state=watch_state,
        chat_state=chat_state,
        slack=slack,
        gchat=gchat,
        chat_events_service=object(), calendar_service=object(),
    )

    assert isinstance(runtime, Runtime)
    assert runtime.app is app
    assert runtime.connector is connector
    assert runtime.gmail_service is gmail_service
    assert runtime.watch_state is watch_state
    assert runtime.chat_state is chat_state
    assert runtime.slack is slack
    assert runtime.gchat is gchat


def test_build_runtime_defaults_watch_and_chat_state_to_json_files(tmp_path):
    watch_path = tmp_path / "watch.json"
    chat_path = tmp_path / "chat.json"
    settings = _settings(
        ADC_GMAIL_WATCH_STATE_PATH=str(watch_path),
        ADC_CHAT_SUBSCRIPTION_STATE_PATH=str(chat_path),
    )

    runtime = build_runtime(
        settings,
        app=_app_ctx(),
        connector=_FakeConnector(),
        gmail_service=_FakeGmailService(),
        chat_events_service=object(), calendar_service=object(),
    )

    assert isinstance(runtime.watch_state, JsonGmailWatchState)
    assert isinstance(runtime.chat_state, JsonChatSubscriptionState)


def test_build_runtime_slack_none_when_no_bot_token():
    settings = _settings()  # no SLACK_BOT_TOKEN
    runtime = build_runtime(
        settings, app=_app_ctx(), connector=_FakeConnector(),
        gmail_service=_FakeGmailService(), chat_events_service=object(), calendar_service=object(),
    )
    assert runtime.slack is None
    assert runtime.slack_say is None


def test_build_runtime_builds_slack_channel_when_bot_token_present():
    settings = _settings(SLACK_BOT_TOKEN="xoxb-token")
    runtime = build_runtime(
        settings, app=_app_ctx(), connector=_FakeConnector(),
        gmail_service=_FakeGmailService(), chat_events_service=object(), calendar_service=object(),
    )
    from aidedecamp.channels import SlackChannel

    assert isinstance(runtime.slack, SlackChannel)
    # No default channel configured -> no say callable yet.
    assert runtime.slack_say is None


def test_build_runtime_builds_slack_say_when_channel_configured():
    settings = _settings(SLACK_BOT_TOKEN="xoxb-token", ADC_SLACK_CHANNEL="C123")
    runtime = build_runtime(
        settings, app=_app_ctx(), connector=_FakeConnector(),
        gmail_service=_FakeGmailService(), chat_events_service=object(), calendar_service=object(),
    )
    assert callable(runtime.slack_say)


def test_build_runtime_wires_slack_message_fn_to_converse():
    client = _FakeClient(reply="here's your answer")
    settings = _settings(SLACK_BOT_TOKEN="xoxb-token")
    runtime = build_runtime(
        settings, app=_app_ctx(client=client), connector=_FakeConnector(),
        gmail_service=_FakeGmailService(), chat_events_service=object(), calendar_service=object(),
    )

    replies = []
    runtime.slack._message("what's on my plate?", "U1", replies.append)

    assert replies == ["here's your answer"]


def test_build_runtime_wires_slack_message_fn_to_brief():
    settings = _settings(SLACK_BOT_TOKEN="xoxb-token")
    connector = _FakeConnector(threads={"t1": _FakeThread()}, events=[])
    client = _FakeClient(reply="Two unread, one meeting.")
    runtime = build_runtime(
        settings, app=_app_ctx(client=client), connector=connector,
        gmail_service=_FakeGmailService(), chat_events_service=object(), calendar_service=object(),
    )

    replies = []
    runtime.slack._message("give me the morning brief", "U1", replies.append)

    assert replies == ["Two unread, one meeting."]


def test_build_runtime_gchat_none_when_no_default_space():
    settings = _settings()  # no ADC_CHAT_SPACE
    runtime = build_runtime(
        settings, app=_app_ctx(), connector=_FakeConnector(),
        gmail_service=_FakeGmailService(), chat_events_service=object(), calendar_service=object(),
    )
    assert runtime.gchat is None


def test_build_runtime_respects_gchat_override_even_with_default_space():
    """When gchat is explicitly injected, make_chat_send_fn must never run —
    it would otherwise attempt a real googleapiclient network call."""
    settings = _settings(ADC_CHAT_SPACE="spaces/ABC")
    fake_gchat = _FakeGChatChannel()
    runtime = build_runtime(
        settings, app=_app_ctx(), connector=_FakeConnector(),
        gmail_service=_FakeGmailService(), gchat=fake_gchat,
        chat_events_service=object(), calendar_service=object(),
    )
    assert runtime.gchat is fake_gchat


def test_build_runtime_builds_gchat_via_make_chat_send_fn(tmp_path):
    """When chat_default_space is set and no gchat override is given,
    build_runtime must construct one via make_chat_send_fn — exercised here
    with a fake googleapiclient.discovery module so no real network call
    happens."""
    calls = []

    class _FakeService:
        def spaces(self):
            class _Spaces:
                def messages(self):
                    class _Messages:
                        def create(self, parent, body):
                            calls.append((parent, body))
                            class _Req:
                                def execute(self_):
                                    return {}
                            return _Req()
                    return _Messages()
            return _Spaces()

    fake_discovery = ModuleType("googleapiclient.discovery")
    fake_discovery.build = lambda *a, **kw: _FakeService()
    fake_googleapiclient = ModuleType("googleapiclient")
    fake_googleapiclient.discovery = fake_discovery

    settings = _settings(ADC_CHAT_SPACE="spaces/ABC")
    with patch.dict(
        sys.modules,
        {"googleapiclient": fake_googleapiclient, "googleapiclient.discovery": fake_discovery},
    ):
        runtime = build_runtime(
            settings, app=_app_ctx(), connector=_FakeConnector(),
            gmail_service=_FakeGmailService(), credentials=object(),
            chat_events_service=object(), calendar_service=object(),
        )

    from aidedecamp.channels import GoogleChatChannel

    assert isinstance(runtime.gchat, GoogleChatChannel)
    runtime.gchat.post_text("spaces/ABC", "hi")
    assert calls == [("spaces/ABC", {"text": "hi"})]


# ---------------------------------------------------------------------------
# process_gmail_notification
# ---------------------------------------------------------------------------


def _runtime(**overrides):
    kwargs = dict(
        app=_app_ctx(),
        connector=_FakeConnector(),
        gmail_service=_FakeGmailService(),
        watch_state=_FakeWatchState(),
        chat_state=_FakeChatState(),
        chat_events_service=object(), calendar_service=object(),
    )
    kwargs.update(overrides)
    return build_runtime(_settings(), **kwargs)


def test_process_gmail_notification_posts_to_both_channels():
    slack = _FakeSlackChannel()
    gchat = _FakeGChatChannel()
    runtime = _runtime(slack=slack, slack_say=lambda **kw: None, gchat=gchat)
    runtime.settings = _settings(ADC_CHAT_SPACE="spaces/ABC")

    result = runtime.process_gmail_notification(
        {"emailAddress": "me", "historyId": "200"}
    )

    assert len(result) == 1
    assert len(slack.approvals) == 1
    assert len(gchat.approvals) == 1
    assert slack.approvals[0]["domain"] == "mail"
    assert gchat.approvals[0]["space"] == "spaces/ABC"


def test_process_gmail_notification_slack_only_when_no_gchat():
    slack = _FakeSlackChannel()
    runtime = _runtime(slack=slack, slack_say=lambda **kw: None, gchat=None)

    runtime.process_gmail_notification({"emailAddress": "me", "historyId": "201"})

    assert len(slack.approvals) == 1


def test_process_gmail_notification_gchat_only_when_no_slack():
    gchat = _FakeGChatChannel()
    runtime = _runtime(slack=None, gchat=gchat)
    runtime.settings = _settings(ADC_CHAT_SPACE="spaces/ABC")

    runtime.process_gmail_notification({"emailAddress": "me", "historyId": "202"})

    assert len(gchat.approvals) == 1


def test_process_gmail_notification_no_channels_configured_still_processes():
    runtime = _runtime(slack=None, gchat=None)
    result = runtime.process_gmail_notification({"emailAddress": "me", "historyId": "203"})
    assert len(result) == 1


def test_process_gmail_notification_uses_settings_user_id():
    graph = _FakeGraph()
    runtime = _runtime(app=_app_ctx(graph=graph))
    runtime.settings = _settings(ADC_USER_ID="someone@example.com")

    runtime.process_gmail_notification({"emailAddress": "me", "historyId": "204"})

    assert graph.calls[0]["state"]["user_id"] == "someone@example.com"


def test_process_gmail_notification_records_audit_log():
    audit_log = _FakeAuditLog()
    graph = _FakeGraph(audit_events=[{"event": "drafted", "ts": "2026-07-10T00:00:00+00:00"}])
    runtime = _runtime(app=_app_ctx(graph=graph, audit_log=audit_log))

    runtime.process_gmail_notification({"emailAddress": "me", "historyId": "205"})

    assert len(audit_log.records) == 1
    assert audit_log.records[0]["domain"] == "mail"


# ---------------------------------------------------------------------------
# process_chat_event
# ---------------------------------------------------------------------------


def _chat_event(text="hello"):
    return {
        "type": "google.workspace.chat.message.v1.created",
        "message": {
            "text": text,
            "argumentText": text,
            "sender": {"name": "users/U1", "type": "HUMAN"},
            "space": {"name": "spaces/ABC"},
        },
    }


def test_process_chat_event_noop_when_gchat_none():
    runtime = _runtime(gchat=None)
    # Must not raise even though there's no channel to post to.
    runtime.process_chat_event(_chat_event("hi"))


def test_process_chat_event_posts_converse_reply():
    gchat = _FakeGChatChannel()
    client = _FakeClient(reply="here's your answer")
    runtime = _runtime(gchat=gchat, app=_app_ctx(client=client))
    runtime.settings = _settings(ADC_CHAT_SPACE="spaces/ABC")

    runtime.process_chat_event(_chat_event("what's on my calendar?"))

    assert gchat.texts == [("spaces/ABC", "here's your answer")]


def test_process_chat_event_posts_brief_on_keyword():
    gchat = _FakeGChatChannel()
    connector = _FakeConnector(threads={"t1": _FakeThread()}, events=[])
    client = _FakeClient(reply="Two unread, one meeting.")
    runtime = _runtime(gchat=gchat, connector=connector, app=_app_ctx(client=client))
    runtime.settings = _settings(ADC_CHAT_SPACE="spaces/ABC")

    runtime.process_chat_event(_chat_event("give me the morning brief"))

    assert len(gchat.texts) == 1
    space, text = gchat.texts[0]
    assert space == "spaces/ABC"
    assert text == "Two unread, one meeting."


# ---------------------------------------------------------------------------
# renew_gmail_watch / renew_chat_subscription
# ---------------------------------------------------------------------------


def test_renew_gmail_watch_uses_settings_user_id_and_topic():
    gmail_service = _FakeGmailService()
    watch_state = _FakeWatchState(email="custom@example.com")
    runtime = _runtime(gmail_service=gmail_service, watch_state=watch_state)
    runtime.settings = _settings(
        ADC_USER_ID="custom@example.com", ADC_GMAIL_PUBSUB_TOPIC="projects/p/topics/t"
    )

    result = runtime.renew_gmail_watch(force=True)

    assert result.email == "custom@example.com"
    assert result.renewed is True


def test_renew_chat_subscription_uses_settings_space_and_topic():
    calls = []

    class _FakeWorkspaceEventsService:
        def subscriptions(self):
            class _Subs:
                def create(self, body):
                    calls.append(body)
                    class _Req:
                        def execute(self_):
                            return {"name": "subscriptions/new", "expireTime": "2026-07-17T00:00:00Z"}
                    return _Req()
            return _Subs()

    runtime = _runtime(chat_events_service=_FakeWorkspaceEventsService())
    runtime.settings = _settings(
        ADC_CHAT_SPACE="spaces/ABC", ADC_CHAT_PUBSUB_TOPIC="projects/p/topics/chat"
    )

    result = runtime.renew_chat_subscription(force=True)

    assert result.space == "spaces/ABC"
    assert calls[0]["targetResource"] == "//chat.googleapis.com/spaces/ABC"


# ---------------------------------------------------------------------------
# build_runtime — Calendar state defaults
# ---------------------------------------------------------------------------


def test_build_runtime_defaults_calendar_state_to_json_files(tmp_path):
    from aidedecamp.ingestion.state import JsonCalendarChannelState, JsonCalendarSyncState

    watch_path = tmp_path / "cal_watch.json"
    sync_path = tmp_path / "cal_sync.json"
    settings = _settings(
        ADC_CALENDAR_WATCH_STATE_PATH=str(watch_path),
        ADC_CALENDAR_SYNC_STATE_PATH=str(sync_path),
    )

    runtime = build_runtime(
        settings, app=_app_ctx(), connector=_FakeConnector(),
        gmail_service=_FakeGmailService(), chat_events_service=object(),
        calendar_service=object(),
    )

    assert isinstance(runtime.calendar_watch_state, JsonCalendarChannelState)
    assert isinstance(runtime.calendar_sync_state, JsonCalendarSyncState)


def test_build_runtime_uses_calendar_service_override():
    calendar_service = object()
    runtime = build_runtime(
        _settings(), app=_app_ctx(), connector=_FakeConnector(),
        gmail_service=_FakeGmailService(), chat_events_service=object(),
        calendar_service=calendar_service,
    )
    assert runtime.calendar_service is calendar_service


# ---------------------------------------------------------------------------
# process_calendar_notification
# ---------------------------------------------------------------------------


class _FakeCalendarEventsService:
    def __init__(self, pages):
        self._pages = pages
        self._i = 0
        self.list_calls: list[dict] = []

    def events(self):
        svc = self

        class _Events:
            def list(self, **kwargs):
                svc.list_calls.append(kwargs)
                class _Req:
                    def execute(self_):
                        page = svc._pages[svc._i]
                        svc._i += 1
                        return page
                return _Req()
        return _Events()


class _FakeCalendarSyncState:
    def __init__(self, initial=None):
        self._data = dict(initial or {})

    def get(self, calendar_id):
        return self._data.get(calendar_id)

    def put(self, calendar_id, *, sync_token):
        self._data[calendar_id] = {"sync_token": sync_token}


def test_process_calendar_notification_reconciles_with_baseline():
    from aidedecamp.ingestion.calendar_sync import CalendarChanges

    sync_state = _FakeCalendarSyncState({"primary": {"sync_token": "old-token"}})
    calendar_service = _FakeCalendarEventsService(pages=[
        {"items": [{"id": "e1"}], "nextSyncToken": "new-token"}
    ])
    runtime = _runtime(
        calendar_service=calendar_service, calendar_sync_state=sync_state,
    )
    runtime.settings = _settings(ADC_CALENDAR_ID="primary")

    result = runtime.process_calendar_notification({"resource_state": "exists"})

    assert isinstance(result, CalendarChanges)
    assert result.event_ids == ["e1"]
    assert sync_state.get("primary")["sync_token"] == "new-token"


def test_process_calendar_notification_full_syncs_on_expired():
    sync_state = _FakeCalendarSyncState()  # no baseline -> SyncExpired
    calendar_service = _FakeCalendarEventsService(pages=[
        {"items": [{"id": "e1"}], "nextSyncToken": "fresh-token"}
    ])
    runtime = _runtime(
        calendar_service=calendar_service, calendar_sync_state=sync_state,
    )
    runtime.settings = _settings(ADC_CALENDAR_ID="primary")

    result = runtime.process_calendar_notification({"resource_state": "sync"})

    assert result.event_ids == ["e1"]
    assert result.next_sync_token == "fresh-token"
    assert sync_state.get("primary")["sync_token"] == "fresh-token"


# ---------------------------------------------------------------------------
# renew_calendar_watch
# ---------------------------------------------------------------------------


class _FakeCalendarWatchService:
    def __init__(self, resource_id="res-1", expire_ms="99999999999999"):
        self._resource_id = resource_id
        self._expire_ms = expire_ms
        self.watch_calls: list = []

    def events(self):
        svc = self

        class _Events:
            def watch(self, calendarId, body):
                svc.watch_calls.append({"calendarId": calendarId, "body": body})
                class _Req:
                    def execute(self_):
                        return {"resourceId": svc._resource_id, "expiration": svc._expire_ms}
                return _Req()
        return _Events()

    def channels(self):
        class _Channels:
            def stop(self, body):
                class _Req:
                    def execute(self_):
                        return {}
                return _Req()
        return _Channels()


class _FakeCalendarWatchState:
    def __init__(self):
        self._data: dict = {}

    def get(self, calendar_id):
        return self._data.get(calendar_id)

    def put(self, calendar_id, *, channel_id, resource_id, expiration):
        self._data[calendar_id] = {
            "channel_id": channel_id, "resource_id": resource_id, "expiration": expiration,
        }


def test_renew_calendar_watch_uses_settings_calendar_id_and_address():
    calendar_service = _FakeCalendarWatchService(resource_id="res-42")
    watch_state = _FakeCalendarWatchState()
    runtime = _runtime(calendar_service=calendar_service, calendar_watch_state=watch_state)
    runtime.settings = _settings(
        ADC_CALENDAR_ID="primary",
        ADC_CALENDAR_WEBHOOK_ADDRESS="https://republisher.example.com/hook",
    )

    result = runtime.renew_calendar_watch(force=True)

    assert result.calendar_id == "primary"
    assert result.resource_id == "res-42"
    assert calendar_service.watch_calls[0]["body"]["address"] == "https://republisher.example.com/hook"
