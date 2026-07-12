"""Tests for runtime.py — the always-on entrypoint's wiring logic.

Only the testable half is exercised here (per the module's own docstring):
``build_runtime``'s override-resolution and ``process_*``/``renew_*``'s
routing. The live loops (``run``, ``run_*_pubsub_loop``) need a real GCP
project and Slack workspace and are excluded by design, same as
``SlackChannel.start()``.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
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

    def get_event(self, event_id):
        for e in self._events:
            if getattr(e, "event_id", None) == event_id:
                return e
        raise KeyError(event_id)

    def create_draft(self, *a, **kw): ...


class _FakeGmailService:
    """One page of history with a single messagesAdded threadId."""

    def __init__(self, thread_ids=("t1",)):
        self._thread_ids = thread_ids

    def users(self):
        tids = self._thread_ids
        profile_hid = getattr(self, "profile_history_id", "999")

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

            def getProfile(self, userId):
                class _Req:
                    def execute(self_):
                        return {"emailAddress": userId, "historyId": profile_hid}
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

    def consolidate(self, *, user_id):
        from aidedecamp.memory.base import ConsolidationReport
        from datetime import datetime, timezone

        return ConsolidationReport(user_id=user_id, ran_at=datetime.now(timezone.utc))


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
        self.briefs: list = []

    def post_approval(self, say, *, thread_id, domain, proposed_draft, rationale=None):
        self.approvals.append(
            {"say": say, "thread_id": thread_id, "domain": domain,
             "proposed_draft": proposed_draft, "rationale": rationale}
        )

    def post_brief(self, say, brief):
        self.briefs.append(brief)


class _FakeGChatChannel:
    def __init__(self):
        self.approvals: list[dict] = []
        self.texts: list[tuple] = []
        self.briefs: list[tuple] = []

    def post_approval(self, space, *, thread_id, domain, proposed_draft, rationale=None):
        self.approvals.append(
            {"space": space, "thread_id": thread_id, "domain": domain,
             "proposed_draft": proposed_draft, "rationale": rationale}
        )

    def post_text(self, space, text):
        self.texts.append((space, text))

    def post_brief(self, space, brief):
        self.briefs.append((space, brief))


def _settings(**overrides):
    base = {
        "ADC_DEPLOYMENT": "personal",
        "ADC_CONNECTOR_MODE": "mcp",
        "ADC_MEM0_URL": "",
        "ADC_AUDIT_LOG_PATH": "",
        # allowlist the fixture actors (deny-by-default since prompt 17)
        "ADC_CHAT_ALLOWED_USERS": "users/U1,users/1,users/tester",
        # Tests use synthetic channel/space ids; acknowledge their visibility.
        "ADC_ACK_DESTINATION_VISIBILITY": "1",
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


def test_slack_actor_uses_canonical_principal_memory_id():
    class _Store(_FakeMemoryStore):
        def __init__(self):
            super().__init__()
            self.user_ids = []

        def search(self, query, *, user_id, limit=5):
            self.user_ids.append(user_id)
            return []

    store = _Store()
    settings = _settings(
        SLACK_BOT_TOKEN="xoxb-token", ADC_USER_ID="owner@example.com"
    )
    app = _app_ctx(client=_FakeClient(), store=store)
    runtime = build_runtime(
        settings, app=app, connector=_FakeConnector(),
        gmail_service=_FakeGmailService(), chat_events_service=object(),
        calendar_service=object(),
    )

    runtime.slack._message("what's next?", "U-SLACK-OWNER", lambda text: None)

    assert store.user_ids == ["owner@example.com"]


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


class _FakePending:
    """Injected so tests never touch the real file-backed default registry."""

    def __init__(self):
        self.registered = []
        self.resolved = []

    def get_pending_for_source(self, source_ref):
        return None

    def register(self, **kw):
        self.registered.append(kw)

    def resolve(self, lg_tid):
        self.resolved.append(lg_tid)

    def pending(self):
        return []


class _FakeConversation:
    """Injected so tests never touch the real file-backed default window."""

    def __init__(self):
        self.appended = []

    def recent(self, *, channel, user_id, now=None):
        return []

    def append(self, *, channel, user_id, role, content, now=None):
        self.appended.append(
            {"channel": channel, "user_id": user_id, "role": role, "content": content}
        )


def _runtime(**overrides):
    kwargs = dict(
        app=_app_ctx(),
        connector=_FakeConnector(),
        gmail_service=_FakeGmailService(),
        watch_state=_FakeWatchState(),
        chat_state=_FakeChatState(),
        chat_events_service=object(), calendar_service=object(),
        pending=_FakePending(),
        conversation=_FakeConversation(),
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


def test_drain_source_retries_replays_gmail_thread(tmp_path):
    from aidedecamp.ingestion import SqliteRetryQueue

    queue = SqliteRetryQueue(str(tmp_path / "retries.db"))
    queue.enqueue(
        "gmail_thread", "t1", {"history_id": "200"}, error="Timeout"
    )
    slack = _FakeSlackChannel()
    runtime = _runtime(
        retry_queue=queue, slack=slack, slack_say=lambda **kw: None
    )

    assert runtime.drain_source_retries() == 1
    assert queue.pending() == []
    assert slack.approvals[0]["thread_id"] == "gmail:t1:200"


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


def _cal_event(event_id, start_offset_min=0, duration_min=30, summary="Meeting"):
    from aidedecamp.connectors.base import CalendarEvent

    base = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    start = base + timedelta(minutes=start_offset_min)
    end = start + timedelta(minutes=duration_min)
    return CalendarEvent(event_id=event_id, summary=summary, start=start, end=end)


def test_process_calendar_notification_reconciles_with_baseline():
    e1 = _cal_event("e1")
    connector = _FakeConnector(events=[e1])
    sync_state = _FakeCalendarSyncState({"primary": {"sync_token": "old-token"}})
    calendar_service = _FakeCalendarEventsService(pages=[
        {"items": [{"id": "e1"}], "nextSyncToken": "new-token"}
    ])
    runtime = _runtime(
        connector=connector, calendar_service=calendar_service, calendar_sync_state=sync_state,
    )
    runtime.settings = _settings(ADC_CALENDAR_ID="primary")

    result = runtime.process_calendar_notification({"resource_state": "exists"})

    assert result == []  # e1 is alone in the window -> no conflict
    assert sync_state.get("primary")["sync_token"] == "new-token"


def test_process_calendar_notification_notifies_on_conflict():
    e1 = _cal_event("e1", 0, duration_min=60, summary="Client call")
    e2 = _cal_event("e2", 15, duration_min=30, summary="Standup")
    connector = _FakeConnector(events=[e1, e2])
    sync_state = _FakeCalendarSyncState({"primary": {"sync_token": "old-token"}})
    calendar_service = _FakeCalendarEventsService(pages=[
        {"items": [{"id": "e1"}], "nextSyncToken": "new-token"}
    ])
    slack_calls = []
    runtime = _runtime(
        connector=connector, calendar_service=calendar_service, calendar_sync_state=sync_state,
        slack_say=lambda **kw: slack_calls.append(kw),
    )
    runtime.settings = _settings(ADC_CALENDAR_ID="primary")

    result = runtime.process_calendar_notification({"resource_state": "exists"})

    assert len(result) == 1
    assert result[0].conflicting_with.event_id == "e2"
    assert len(slack_calls) == 1
    assert "Client call" in slack_calls[0]["text"]


def test_process_calendar_notification_full_syncs_on_expired():
    e1 = _cal_event("e1")
    connector = _FakeConnector(events=[e1])
    sync_state = _FakeCalendarSyncState()  # no baseline -> SyncExpired
    calendar_service = _FakeCalendarEventsService(pages=[
        {"items": [{"id": "e1"}], "nextSyncToken": "fresh-token"}
    ])
    runtime = _runtime(
        connector=connector, calendar_service=calendar_service, calendar_sync_state=sync_state,
    )
    runtime.settings = _settings(ADC_CALENDAR_ID="primary")

    result = runtime.process_calendar_notification({"resource_state": "sync"})

    assert result == []
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


# ---------------------------------------------------------------------------
# process_chat_interaction — the async half of Chat's approve/reject flow
# ---------------------------------------------------------------------------


def _interaction_click(fn: str, thread_id: str = "t-1") -> dict:
    return {
        "type": "CARD_CLICKED",
        "user": {"name": "users/tester"},
        "action": {
            "actionMethodName": fn,
            "parameters": [{"key": "thread_id", "value": thread_id}],
        },
    }


def test_process_chat_interaction_noop_when_gchat_none():
    runtime = _runtime(gchat=None)
    # Must not raise even though there's no channel to post to.
    runtime.process_chat_interaction(_interaction_click("adc_approve", "t-1"))


def test_process_chat_interaction_resumes_and_posts_confirmation():
    graph = _FakeGraph()
    gchat = _FakeGChatChannel()
    runtime = _runtime(app=_app_ctx(graph=graph), gchat=gchat)
    runtime.settings = _settings(ADC_CHAT_SPACE="spaces/ABC")

    runtime.process_chat_interaction(_interaction_click("adc_approve", "t-42"))

    assert len(graph.calls) == 1
    # The fake graph's final state has no applied_ref, so the honest
    # confirmation claims nothing beyond the recorded decision (prompt 01:
    # never announce a materialization that didn't happen).
    assert gchat.texts == [("spaces/ABC", "✅ Approved.")]


def test_process_chat_interaction_reject_posts_rejection_confirmation():
    graph = _FakeGraph()
    gchat = _FakeGChatChannel()
    runtime = _runtime(app=_app_ctx(graph=graph), gchat=gchat)
    runtime.settings = _settings(ADC_CHAT_SPACE="spaces/ABC")

    runtime.process_chat_interaction(_interaction_click("adc_reject", "t-9"))

    assert gchat.texts == [("spaces/ABC", "🗑️ Rejected — nothing sent.")]


# ---------------------------------------------------------------------------
# poll_once (prompt 09) — timer-driven ingestion through the same dispatcher
# ---------------------------------------------------------------------------


class _FakeChatPollService:
    def __init__(self, messages=None):
        self._messages = messages or []

    def spaces(self):
        outer = self

        class _Messages:
            def list(self, **kwargs):
                class _Req:
                    def execute(self_):
                        return {"messages": outer._messages}
                return _Req()

        class _Spaces:
            def messages(self):
                return _Messages()

        return _Spaces()


class _DictChatPollState:
    def __init__(self, last_seen=None):
        self._data = {"spaces/ABC": {"last_seen": last_seen}} if last_seen else {}
        self.puts: list[str] = []

    def get(self, space):
        return self._data.get(space)

    def put(self, space, *, last_seen):
        self._data[space] = {"last_seen": last_seen}
        self.puts.append(last_seen)


def test_poll_once_gmail_dispatches_when_mailbox_advanced():
    slack = _FakeSlackChannel()
    runtime = _runtime(slack=slack, slack_say=lambda **kw: None)
    # profile historyId (999) is ahead of the stored baseline (100)

    summary = runtime.poll_once()

    assert summary["gmail"] == "changed"
    assert len(slack.approvals) == 1  # same dispatcher path as push mode


def test_poll_once_gmail_idle_when_no_change():
    gmail = _FakeGmailService()
    gmail.profile_history_id = "100"  # equals the stored baseline
    slack = _FakeSlackChannel()
    runtime = _runtime(gmail_service=gmail, slack=slack,
                       slack_say=lambda **kw: None)

    summary = runtime.poll_once()

    assert summary["gmail"] == "idle"
    assert slack.approvals == []


def test_poll_once_chat_dispatches_and_advances_mark_after_success():
    gchat = _FakeGChatChannel()
    msg = {
        "name": "spaces/ABC/messages/m1",
        "text": "what's up?",
        "createTime": "2026-07-10T12:00:00Z",
        "sender": {"name": "users/1", "type": "HUMAN"},
        "space": {"name": "spaces/ABC"},
    }
    poll_state = _DictChatPollState(last_seen="2026-07-10T11:00:00Z")
    runtime = _runtime(
        gchat=gchat,
        chat_service=_FakeChatPollService([msg]),
        chat_poll_state=poll_state,
    )
    runtime.settings = _settings(ADC_CHAT_SPACE="spaces/ABC")

    summary = runtime.poll_once()

    assert summary["chat"] == "1 message(s)"
    assert len(gchat.texts) == 1  # conversational reply posted
    assert poll_state.puts == ["2026-07-10T12:00:00Z"]


def test_poll_once_isolates_source_failures():
    """A broken calendar service must not stop gmail/chat polling."""
    slack = _FakeSlackChannel()
    runtime = _runtime(
        slack=slack, slack_say=lambda **kw: None,
        calendar_service=object(),  # unusable -> calendar step errors
    )

    summary = runtime.poll_once()

    assert summary["gmail"] == "changed"
    assert summary["calendar"].startswith("error:")
    assert len(slack.approvals) == 1


# ---------------------------------------------------------------------------
# Supervised pull-loop machinery (prompt 06) — the testable per-message core
# ---------------------------------------------------------------------------


def test_malformed_message_is_poison_not_fatal():
    """Malformed JSON → logged by id, audited, reported unhandled — never a
    raise (a raise is what used to kill the daemon thread silently)."""
    audit = _FakeAuditLog()
    runtime = _runtime(app=_app_ctx(audit_log=audit))

    ok = runtime._handle_pulled_message(
        "gmail", b"not json{", "msg-123", lambda payload: None
    )

    assert ok is False
    event = audit.records[0]["events"][0]
    assert event["event"] == "message_failed"
    assert event["message_id"] == "msg-123"
    assert event["error"] == "malformed_json"


def test_raising_handler_is_poison_not_fatal():
    audit = _FakeAuditLog()
    runtime = _runtime(app=_app_ctx(audit_log=audit))

    def boom(payload):
        raise ValueError("deterministic failure")

    ok = runtime._handle_pulled_message("chat", b'{"a": 1}', "msg-9", boom)

    assert ok is False
    assert audit.records[0]["events"][0]["error"] == "ValueError"


def test_successful_handler_reports_handled():
    runtime = _runtime()
    seen = []
    ok = runtime._handle_pulled_message(
        "chat", b'{"a": 1}', "msg-1", seen.append
    )
    assert ok is True
    assert seen == [{"a": 1}]


def test_failure_log_never_contains_payload(caplog):
    """Rule 6: a poison message is logged by id only — its body (which could
    contain tokens or mail content) must never enter the log stream."""
    import logging as _logging

    runtime = _runtime()
    secret_body = b'{"token": "SECRET-FUELIX-TOKEN-VALUE"}'

    def boom(payload):
        raise RuntimeError("handler down")

    with caplog.at_level(_logging.DEBUG):
        runtime._handle_pulled_message("gmail", secret_body, "msg-7", boom)
        runtime._handle_pulled_message("gmail", b"SECRET-RAW not json", "msg-8", boom)

    assert caplog.records  # something was logged
    for record in caplog.records:
        assert "SECRET" not in record.getMessage()


def test_gmail_handler_renews_on_history_expired():
    from aidedecamp.ingestion import HistoryExpired

    runtime = _runtime()
    renewed = []
    runtime.renew_gmail_watch = lambda *, force=False: renewed.append(force)

    def expired(payload):
        raise HistoryExpired("stale baseline")

    runtime.process_gmail_notification = expired
    runtime._handle_gmail_message({"emailAddress": "me", "historyId": "1"})

    assert renewed == [True]


def test_next_backoff_doubles_and_caps():
    from aidedecamp.runtime import next_backoff

    seq = [1]
    for _ in range(8):
        seq.append(next_backoff(seq[-1]))
    assert seq == [1, 2, 4, 8, 16, 32, 60, 60, 60]


def test_loop_stats_heartbeat_cadence_and_reset():
    from aidedecamp.runtime import LoopStats

    t0 = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    stats = LoopStats("gmail", interval_seconds=300)

    assert stats.maybe_beat(t0) is None  # first call arms the timer
    stats.record(ok=True)
    stats.record(ok=True)
    stats.record(ok=False)
    assert stats.maybe_beat(t0 + timedelta(seconds=299)) is None

    line = stats.maybe_beat(t0 + timedelta(seconds=301))
    assert line == "heartbeat gmail: pulled=3 handled=2 failed=1"
    # counters reset after the beat
    assert stats.maybe_beat(t0 + timedelta(seconds=602)) == (
        "heartbeat gmail: pulled=0 handled=0 failed=0"
    )


def test_logging_setup_json_mode_emits_json_lines():
    import json as _json
    import logging as _logging

    from aidedecamp.logging_setup import JsonFormatter

    record = _logging.LogRecord(
        name="aidedecamp.runtime", level=_logging.INFO, pathname=__file__,
        lineno=1, msg="heartbeat gmail: pulled=%d", args=(3,), exc_info=None,
    )
    entry = _json.loads(JsonFormatter().format(record))
    assert entry["level"] == "INFO"
    assert entry["logger"] == "aidedecamp.runtime"
    assert entry["message"] == "heartbeat gmail: pulled=3"
    assert "ts" in entry


def test_logging_setup_configure_is_idempotent():
    import logging as _logging

    from aidedecamp.logging_setup import configure

    configure(level="WARNING")
    configure(level="DEBUG", json_mode=True)
    root = _logging.getLogger()
    assert len(root.handlers) == 1  # replaced, not stacked
    assert root.level == _logging.DEBUG
    # restore something sane for other tests
    configure(level="WARNING")


def test_build_scheduler_assembles_expected_jobs():
    """The standard job set (prompts 05 + 09): brief only when a channel can
    carry it, renewals only in push mode (poll mode has no watches to renew),
    sweep when a registry exists, consolidation always."""
    runtime = _runtime(slack=_FakeSlackChannel(), slack_say=lambda **kw: None)
    names = [j.name for j in runtime.build_scheduler().jobs]
    # default mode is poll -> no renew_watches job
    assert names == [
        "daily_brief", "sweep_pending", "source_retries", "consolidate",
        "autonomy_digest",
    ]

    # push mode gets the daily renewal job
    push = _runtime(slack=_FakeSlackChannel(), slack_say=lambda **kw: None)
    push.settings = _settings(ADC_INGESTION_MODE="push")
    names = [j.name for j in push.build_scheduler().jobs]
    assert names == [
        "daily_brief", "renew_watches", "sweep_pending", "source_retries",
        "consolidate", "autonomy_digest",
    ]

    # A real user address + nudge state -> the daily nudge job appears.
    nudging = _runtime(slack=_FakeSlackChannel(), slack_say=lambda **kw: None,
                       nudge_state=object())
    nudging.settings = _settings(ADC_USER_ID="me@example.com")
    names = [j.name for j in nudging.build_scheduler().jobs]
    assert "follow_up_nudges" in names

    # No channel configured -> no brief job (nowhere to post it).
    quiet = _runtime()
    names = [j.name for j in quiet.build_scheduler().jobs]
    assert "daily_brief" not in names
    assert "follow_up_nudges" not in names  # user_id "me" can't detect quiet

    # No registry -> no sweep job.
    no_pending = _runtime(pending=None)
    no_pending.pending = None
    names = [j.name for j in no_pending.build_scheduler().jobs]
    assert "sweep_pending" not in names


def test_scheduler_brief_job_uses_configured_time_and_tz():
    from datetime import datetime, timezone as _tz

    runtime = _runtime(slack=_FakeSlackChannel(), slack_say=lambda **kw: None)
    runtime.settings = _settings(ADC_BRIEF_TIME="06:15", ADC_TIMEZONE="America/Vancouver")
    scheduler = runtime.build_scheduler()

    # First tick schedules; 06:15 Vancouver in July (PDT) is 13:15 UTC.
    t0 = datetime(2026, 7, 10, 1, 0, tzinfo=_tz.utc)
    scheduler.run_pending(t0)
    assert scheduler.next_run("daily_brief") == datetime(
        2026, 7, 10, 13, 15, tzinfo=_tz.utc
    )


def test_post_brief_posts_to_both_channels():
    slack = _FakeSlackChannel()
    gchat = _FakeGChatChannel()
    runtime = _runtime(slack=slack, slack_say=lambda **kw: None, gchat=gchat)
    runtime.settings = _settings(ADC_CHAT_SPACE="spaces/ABC")

    brief = runtime.post_brief()

    assert brief.summary  # assembled from the fake connector/client
    assert len(slack.briefs) == 1
    assert len(gchat.briefs) == 1


def test_renew_all_watches_isolates_and_audits_failures():
    """One failing renewal must not skip the rest, and both outcomes are
    audited under the ops workflow (prompt 05)."""
    audit = _FakeAuditLog()
    runtime = _runtime(app=_app_ctx(audit_log=audit))
    runtime.settings = _settings(
        ADC_GMAIL_PUBSUB_TOPIC="projects/p/topics/gmail",
        ADC_CHAT_PUBSUB_TOPIC="projects/p/topics/chat",
        ADC_CHAT_SPACE="spaces/ABC",
    )

    def boom(**kw):
        raise RuntimeError("watch API down")

    runtime.renew_gmail_watch = boom
    renewed = []
    runtime.renew_chat_subscription = lambda **kw: renewed.append("chat")

    results = runtime.renew_all_watches()

    assert results["gmail_watch"].startswith("failed")
    assert results["chat_subscription"] == "renewed"
    assert renewed == ["chat"]  # failure upstream didn't skip it
    events = [r["events"][0]["event"] for r in audit.records]
    assert events == ["renewal_failed", "watch_renewed"]
    assert all(r["workflow"] == "ops" for r in audit.records)


def test_renew_all_watches_skips_unconfigured_sources():
    runtime = _runtime()  # _settings() sets no topics/spaces/webhook
    assert runtime.renew_all_watches() == {}


def test_run_consolidation_audits_report():
    audit = _FakeAuditLog()
    runtime = _runtime(app=_app_ctx(audit_log=audit))

    report = runtime.run_consolidation()

    assert report is not None
    rec = audit.records[0]
    assert rec["workflow"] == "ops"
    assert rec["events"][0]["event"] == "consolidation_ran"


def test_consolidation_internal_typeerror_is_not_retried():
    class _Store(_FakeMemoryStore):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def consolidate(self, *, user_id, audit_log=None):
            self.calls += 1
            raise TypeError("raised inside consolidation")

    store = _Store()
    runtime = _runtime(app=_app_ctx(store=store))
    with pytest.raises(TypeError, match="inside consolidation"):
        runtime.run_consolidation()
    assert store.calls == 1


def test_weekly_autonomy_digest_posts_suggestions_to_channels():
    """The digest posts earned-graduation suggestions (information only —
    grants stay CLI-only, prompt 12) to every configured channel."""
    from datetime import datetime, timezone as _tz

    from aidedecamp.audit.log import JsonlAuditLog
    import tempfile, os

    with tempfile.TemporaryDirectory() as td:
        log = JsonlAuditLog(os.path.join(td, "audit.jsonl"))
        now = datetime.now(_tz.utc).isoformat()
        for i in range(12):
            tid = f"gmail:t{i}:100"
            log.record(
                thread_id=tid, workflow="draft_approve",
                events=[{"event": "autonomy_gate", "ts": now,
                         "action": "draft_reply", "domain": "mail",
                         "routed_to": "approve"}],
                domain="mail", user_id="u1",
            )
            log.record(
                thread_id=tid, workflow="draft_approve",
                events=[
                    {"event": "human_decision", "ts": now,
                     "decision": "approved"},
                    {"event": "applied", "ts": now, "ref": f"d{i}"},
                ],
                domain="mail", user_id="u1",
            )

        said: list[dict] = []
        gchat = _FakeGChatChannel()
        runtime = _runtime(
            app=_app_ctx(audit_log=log),
            slack=_FakeSlackChannel(),
            slack_say=lambda **kw: said.append(kw),
            gchat=gchat,
        )
        runtime.settings = _settings(ADC_CHAT_SPACE="spaces/ABC")

        suggestions = runtime.post_autonomy_digest()

    assert len(suggestions) == 1
    assert "12/12" in said[0]["text"]
    assert "aidedecamp autonomy grant" in said[0]["text"]
    assert len(gchat.texts) == 1


def test_autonomy_digest_silent_when_nothing_earned():
    said: list[dict] = []
    runtime = _runtime(slack_say=lambda **kw: said.append(kw))
    assert runtime.post_autonomy_digest() == []
    assert said == []


def test_gmail_notification_registers_pending_card():
    """process_gmail_notification threads the registry through the dispatcher
    (prompt 03): a posted card lands in the registry."""
    pending = _FakePending()
    runtime = _runtime(
        slack=_FakeSlackChannel(), slack_say=lambda **kw: None, pending=pending
    )

    runtime.process_gmail_notification({"emailAddress": "me", "historyId": "200"})

    assert len(pending.registered) == 1
    assert pending.registered[0]["domain"] == "mail"


def test_sweep_pending_ignored_uses_settings_threshold(tmp_path):
    """sweep_pending_ignored wires registry + store + audit log + the
    configured ignore-hours threshold together (prompt 03)."""
    from datetime import datetime, timedelta, timezone

    from aidedecamp.orchestrator import JsonPendingApprovals

    registry = JsonPendingApprovals(str(tmp_path / "pending.json"))
    t0 = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    registry.register(
        lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=t0
    )
    store = _FakeMemoryStore()
    runtime = _runtime(app=_app_ctx(store=store), pending=registry)

    # Default threshold is 48h: 47h in → nothing; 49h in → swept.
    assert runtime.sweep_pending_ignored(now=t0 + timedelta(hours=47)) == 0
    assert runtime.sweep_pending_ignored(now=t0 + timedelta(hours=49)) == 1
    assert runtime.sweep_pending_ignored(now=t0 + timedelta(hours=50)) == 0


def test_sweep_pending_ignored_noop_without_registry():
    runtime = _runtime(pending=None)
    # build_runtime substitutes the real default when passed None, so force it:
    runtime.pending = None
    assert runtime.sweep_pending_ignored() == 0


def test_process_chat_interaction_records_audit_log():
    graph = _FakeGraph()
    audit_log = _FakeAuditLog()
    gchat = _FakeGChatChannel()
    runtime = _runtime(app=_app_ctx(graph=graph, audit_log=audit_log), gchat=gchat)
    runtime.settings = _settings(ADC_CHAT_SPACE="spaces/ABC")

    runtime.process_chat_interaction(_interaction_click("adc_approve", "t-42"))

    assert len(audit_log.records) == 1
    assert audit_log.records[0]["domain"] == "chat"
    assert audit_log.records[0]["thread_id"] == "t-42"


def test_process_chat_interaction_ignores_edit():
    graph = _FakeGraph()
    gchat = _FakeGChatChannel()
    runtime = _runtime(app=_app_ctx(graph=graph), gchat=gchat)
    runtime.settings = _settings(ADC_CHAT_SPACE="spaces/ABC")

    runtime.process_chat_interaction(_interaction_click("adc_edit", "t-1"))

    assert graph.calls == []
    assert gchat.texts == []
