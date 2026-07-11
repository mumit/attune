"""Tests for dispatcher.py — no live services, no LLM calls.

All collaborators are injected fakes. The fake graph stubs the LangGraph
draft-approve workflow so no langgraph is needed for the dispatcher tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from aidedecamp.dispatcher import (
    handle_chat_interaction,
    handle_chat_message,
    handle_gmail_notification,
    handle_slack_message,
    _converse,
)
from aidedecamp.orchestrator.triage import Priority, TriageResult


# ---------------------------------------------------------------------------
# Fake collaborators
# ---------------------------------------------------------------------------

class _FakeThread:
    def __init__(self, thread_id="t1", subject="Hello", from_addr="a@b.com", body="body text"):
        self.thread_id = thread_id
        self.subject = subject
        self.from_addr = from_addr
        self.body = body
        self.snippet = body[:20]
        self.labels = []
        self.received_at = None
        from aidedecamp.connectors.base import Provenance
        self.provenance = Provenance.FETCHED


class _FakeConnector:
    def __init__(self, threads: dict | None = None):
        self._threads = threads if threads is not None else {}

    def get_thread(self, thread_id):
        if thread_id not in self._threads:
            raise KeyError(thread_id)
        return self._threads[thread_id]

    def list_threads(self, *a, **kw): return []
    def list_events(self, *a, **kw): return []
    def create_draft(self, *a, **kw): ...


class _FakeWatchState:
    def __init__(self, email="me@example.com", history_id="100"):
        self._data = {email: {"history_id": history_id, "expiration": datetime.now(timezone.utc)}}

    def get(self, email):
        return self._data.get(email)

    def put(self, email, *, history_id, expiration):
        self._data[email] = {"history_id": history_id, "expiration": expiration}


class _FakeGmail:
    """Fake Gmail service that returns one 'messagesAdded' history record."""

    def __init__(self, thread_ids: list[str]):
        self._thread_ids = thread_ids

    def users(self):
        tids = self._thread_ids

        class _History:
            def list(self, **kwargs):
                class _Req:
                    def execute(self):
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

        return _Users()


class _FakeGraph:
    """Fake LangGraph compiled graph that immediately returns a proposed_draft."""

    def __init__(self, proposed="draft text", memories=None, audit_events=None):
        self._proposed = proposed
        self._memories = memories or []
        self._audit_events = audit_events or [
            {"event": "retrieved", "ts": "2026-07-10T00:00:00+00:00"},
            {"event": "drafted", "ts": "2026-07-10T00:00:01+00:00"},
        ]
        self.calls: list[dict] = []

    def invoke(self, state, config):
        self.calls.append({"state": state, "config": config})
        return {
            "proposed_draft": self._proposed,
            "retrieved_memories": self._memories,
            "audit_events": self._audit_events,
        }


class _FakeAuditLog:
    def __init__(self):
        self.records: list[dict] = []

    def record(self, **kwargs):
        self.records.append(kwargs)

    def query(self, **kwargs):
        return []


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


def _fake_app_ctx(graph=None, store=None, client=None, audit_log=None):
    from aidedecamp.app import AppContext
    from aidedecamp.config import Settings
    s = Settings.from_env({"ADC_DEPLOYMENT": "personal", "ADC_CONNECTOR_MODE": "mcp",
                            "ADC_MEM0_URL": "", "ADC_AUDIT_LOG_PATH": ""})
    return AppContext(
        graph=graph or _FakeGraph(),
        client=client or _FakeClient(),
        store=store or _FakeMemoryStore(),
        settings=s,
        audit_log=audit_log or _FakeAuditLog(),
    )


# ---------------------------------------------------------------------------
# handle_gmail_notification
# ---------------------------------------------------------------------------

def test_handle_gmail_notification_submits_one_workflow():
    graph = _FakeGraph(proposed="please confirm")
    app = _fake_app_ctx(graph=graph)
    connector = _FakeConnector({"t1": _FakeThread("t1")})
    gmail = _FakeGmail(["t1"])
    watch_state = _FakeWatchState(history_id="100")
    approvals = []

    notification = {"emailAddress": "me@example.com", "historyId": "200"}
    result = handle_gmail_notification(
        app, notification,
        gmail_service=gmail,
        watch_state=watch_state,
        connector=connector,
        post_approval=lambda tid, draft, rationale: approvals.append((tid, draft, rationale)),
        user_id="me@example.com",
    )

    assert len(result) == 1
    assert result[0].startswith("gmail:t1:200")
    assert len(approvals) == 1
    tid, draft, rationale = approvals[0]
    assert draft == "please confirm"
    assert tid == result[0]


def test_handle_gmail_notification_skips_missing_thread():
    graph = _FakeGraph()
    app = _fake_app_ctx(graph=graph)
    connector = _FakeConnector({})  # no threads → get_thread raises
    gmail = _FakeGmail(["t1"])
    watch_state = _FakeWatchState(history_id="100")
    approvals = []

    result = handle_gmail_notification(
        app, {"emailAddress": "me@example.com", "historyId": "201"},
        gmail_service=gmail, watch_state=watch_state,
        connector=connector,
        post_approval=lambda *a: approvals.append(a),
        user_id="me@example.com",
    )

    assert result == []
    assert approvals == []


def test_handle_gmail_notification_multiple_threads():
    graph = _FakeGraph(proposed="draft")
    app = _fake_app_ctx(graph=graph)
    connector = _FakeConnector({
        "t1": _FakeThread("t1"),
        "t2": _FakeThread("t2"),
    })
    gmail = _FakeGmail(["t1", "t2"])
    watch_state = _FakeWatchState(history_id="100")
    approvals = []

    result = handle_gmail_notification(
        app, {"emailAddress": "me@example.com", "historyId": "300"},
        gmail_service=gmail, watch_state=watch_state,
        connector=connector,
        post_approval=lambda *a: approvals.append(a),
        user_id="me@example.com",
    )

    assert len(result) == 2
    assert len(approvals) == 2


def test_gmail_lg_thread_id_includes_prefix_and_history_id():
    graph = _FakeGraph()
    app = _fake_app_ctx(graph=graph)
    connector = _FakeConnector({"t1": _FakeThread("t1")})
    gmail = _FakeGmail(["t1"])
    watch_state = _FakeWatchState(history_id="100")
    result = handle_gmail_notification(
        app, {"emailAddress": "me@example.com", "historyId": "999"},
        gmail_service=gmail, watch_state=watch_state,
        connector=connector,
        post_approval=lambda *a: None,
        user_id="me@example.com",
        thread_id_prefix="myprefix",
    )
    assert result[0] == "myprefix:t1:999"


def test_gmail_graph_invoked_with_correct_config():
    graph = _FakeGraph()
    app = _fake_app_ctx(graph=graph)
    connector = _FakeConnector({"t1": _FakeThread("t1", subject="Sub", from_addr="x@y.com")})
    gmail = _FakeGmail(["t1"])
    watch_state = _FakeWatchState(history_id="100")
    handle_gmail_notification(
        app, {"emailAddress": "me@example.com", "historyId": "202"},
        gmail_service=gmail, watch_state=watch_state,
        connector=connector,
        post_approval=lambda *a: None,
        user_id="me@example.com",
    )
    assert len(graph.calls) == 1
    call = graph.calls[0]
    assert call["config"]["configurable"]["thread_id"].startswith("gmail:t1:")
    assert "Sub" in call["state"]["incoming_summary"]
    assert "x@y.com" in call["state"]["incoming_summary"]


def test_gmail_graph_state_carries_thread_ref_for_apply():
    """The Gmail thread id rides in as incoming_ref so the graph's apply step
    can create the reply draft against the right thread (prompt 01)."""
    graph = _FakeGraph()
    app = _fake_app_ctx(graph=graph)
    connector = _FakeConnector({"t1": _FakeThread("t1")})
    gmail = _FakeGmail(["t1"])
    watch_state = _FakeWatchState(history_id="100")
    handle_gmail_notification(
        app, {"emailAddress": "me@example.com", "historyId": "202"},
        gmail_service=gmail, watch_state=watch_state,
        connector=connector,
        post_approval=lambda *a: None,
        user_id="me@example.com",
    )
    assert graph.calls[0]["state"]["incoming_ref"] == "t1"


def test_handle_gmail_rationale_passed_through():
    mems = ["prefers short replies"]
    graph = _FakeGraph(proposed="short reply", memories=mems)
    app = _fake_app_ctx(graph=graph)
    connector = _FakeConnector({"t1": _FakeThread("t1")})
    gmail = _FakeGmail(["t1"])
    watch_state = _FakeWatchState(history_id="100")
    approvals = []
    handle_gmail_notification(
        app, {"emailAddress": "me@example.com", "historyId": "500"},
        gmail_service=gmail, watch_state=watch_state,
        connector=connector,
        post_approval=lambda tid, draft, rat: approvals.append((tid, draft, rat)),
        user_id="me@example.com",
    )
    assert approvals[0][2] == mems


# ---------------------------------------------------------------------------
# handle_gmail_notification — audit_log wiring
# ---------------------------------------------------------------------------


def test_gmail_notification_records_audit_events_when_log_provided():
    audit_log = _FakeAuditLog()
    graph = _FakeGraph(audit_events=[{"event": "drafted", "ts": "2026-07-10T00:00:00+00:00"}])
    app = _fake_app_ctx(graph=graph, audit_log=audit_log)
    connector = _FakeConnector({"t1": _FakeThread("t1")})
    gmail = _FakeGmail(["t1"])
    watch_state = _FakeWatchState(history_id="100")

    result = handle_gmail_notification(
        app, {"emailAddress": "me@example.com", "historyId": "600"},
        gmail_service=gmail, watch_state=watch_state,
        connector=connector,
        post_approval=lambda *a: None,
        user_id="me@example.com",
        audit_log=audit_log,
    )

    assert len(audit_log.records) == 1
    rec = audit_log.records[0]
    assert rec["thread_id"] == result[0]
    assert rec["workflow"] == "draft_approve"
    assert rec["domain"] == "mail"
    assert rec["user_id"] == "me@example.com"
    assert rec["events"] == [{"event": "drafted", "ts": "2026-07-10T00:00:00+00:00"}]


def test_gmail_notification_no_audit_calls_when_log_absent():
    graph = _FakeGraph()
    app = _fake_app_ctx(graph=graph)
    connector = _FakeConnector({"t1": _FakeThread("t1")})
    gmail = _FakeGmail(["t1"])
    watch_state = _FakeWatchState(history_id="100")

    # audit_log intentionally omitted — should not raise, no recording call.
    handle_gmail_notification(
        app, {"emailAddress": "me@example.com", "historyId": "601"},
        gmail_service=gmail, watch_state=watch_state,
        connector=connector,
        post_approval=lambda *a: None,
        user_id="me@example.com",
    )


def test_gmail_notification_audit_skipped_for_failed_thread_fetch():
    audit_log = _FakeAuditLog()
    graph = _FakeGraph()
    app = _fake_app_ctx(graph=graph, audit_log=audit_log)
    connector = _FakeConnector({})  # get_thread raises for all
    gmail = _FakeGmail(["t1"])
    watch_state = _FakeWatchState(history_id="100")

    handle_gmail_notification(
        app, {"emailAddress": "me@example.com", "historyId": "602"},
        gmail_service=gmail, watch_state=watch_state,
        connector=connector,
        post_approval=lambda *a: None,
        user_id="me@example.com",
        audit_log=audit_log,
    )

    assert audit_log.records == []


# ---------------------------------------------------------------------------
# handle_gmail_notification — triage gate
# ---------------------------------------------------------------------------


def test_noise_thread_skips_draft_and_post_approval():
    graph = _FakeGraph()
    app = _fake_app_ctx(graph=graph)
    connector = _FakeConnector({"t1": _FakeThread("t1")})
    gmail = _FakeGmail(["t1"])
    watch_state = _FakeWatchState(history_id="100")
    approvals = []

    result = handle_gmail_notification(
        app, {"emailAddress": "me@example.com", "historyId": "700"},
        gmail_service=gmail, watch_state=watch_state,
        connector=connector,
        post_approval=lambda *a: approvals.append(a),
        user_id="me@example.com",
        triage_fn=lambda client, summary: TriageResult(Priority.NOISE, "newsletter"),
    )

    assert result == []
    assert approvals == []
    assert graph.calls == []


def test_urgent_thread_proceeds_to_draft():
    graph = _FakeGraph(proposed="drafted reply")
    app = _fake_app_ctx(graph=graph)
    connector = _FakeConnector({"t1": _FakeThread("t1")})
    gmail = _FakeGmail(["t1"])
    watch_state = _FakeWatchState(history_id="100")
    approvals = []

    result = handle_gmail_notification(
        app, {"emailAddress": "me@example.com", "historyId": "701"},
        gmail_service=gmail, watch_state=watch_state,
        connector=connector,
        post_approval=lambda *a: approvals.append(a),
        user_id="me@example.com",
        triage_fn=lambda client, summary: TriageResult(Priority.URGENT, "escalation"),
    )

    assert len(result) == 1
    assert len(approvals) == 1


def test_routine_thread_proceeds_to_draft():
    graph = _FakeGraph()
    app = _fake_app_ctx(graph=graph)
    connector = _FakeConnector({"t1": _FakeThread("t1")})
    gmail = _FakeGmail(["t1"])
    watch_state = _FakeWatchState(history_id="100")

    result = handle_gmail_notification(
        app, {"emailAddress": "me@example.com", "historyId": "702"},
        gmail_service=gmail, watch_state=watch_state,
        connector=connector,
        post_approval=lambda *a: None,
        user_id="me@example.com",
        triage_fn=lambda client, summary: TriageResult(Priority.ROUTINE, "fine"),
    )

    assert len(result) == 1


def test_noise_thread_records_triage_audit_event():
    audit_log = _FakeAuditLog()
    graph = _FakeGraph()
    app = _fake_app_ctx(graph=graph, audit_log=audit_log)
    connector = _FakeConnector({"t1": _FakeThread("t1")})
    gmail = _FakeGmail(["t1"])
    watch_state = _FakeWatchState(history_id="100")

    handle_gmail_notification(
        app, {"emailAddress": "me@example.com", "historyId": "703"},
        gmail_service=gmail, watch_state=watch_state,
        connector=connector,
        post_approval=lambda *a: None,
        user_id="me@example.com",
        audit_log=audit_log,
        triage_fn=lambda client, summary: TriageResult(Priority.NOISE, "automated digest"),
    )

    assert len(audit_log.records) == 1
    rec = audit_log.records[0]
    assert rec["workflow"] == "triage"
    assert rec["domain"] == "mail"
    assert rec["events"][0]["event"] == "triaged_noise"
    assert rec["events"][0]["reason"] == "automated digest"


def test_no_audit_call_for_noise_when_log_absent():
    graph = _FakeGraph()
    app = _fake_app_ctx(graph=graph)
    connector = _FakeConnector({"t1": _FakeThread("t1")})
    gmail = _FakeGmail(["t1"])
    watch_state = _FakeWatchState(history_id="100")

    # Must not raise even though no audit_log is provided.
    handle_gmail_notification(
        app, {"emailAddress": "me@example.com", "historyId": "704"},
        gmail_service=gmail, watch_state=watch_state,
        connector=connector,
        post_approval=lambda *a: None,
        user_id="me@example.com",
        triage_fn=lambda client, summary: TriageResult(Priority.NOISE, "spam"),
    )


def test_default_triage_fn_uses_real_triage_thread():
    """Without an override, handle_gmail_notification uses the real
    triage_thread — a malformed/unrelated model response must default to
    ROUTINE so the FakeClient's canned reply doesn't accidentally suppress
    real mail."""
    graph = _FakeGraph(proposed="a reply")
    client = _FakeClient(reply="not a real classification response")
    app = _fake_app_ctx(graph=graph, client=client)
    connector = _FakeConnector({"t1": _FakeThread("t1")})
    gmail = _FakeGmail(["t1"])
    watch_state = _FakeWatchState(history_id="100")

    result = handle_gmail_notification(
        app, {"emailAddress": "me@example.com", "historyId": "705"},
        gmail_service=gmail, watch_state=watch_state,
        connector=connector,
        post_approval=lambda *a: None,
        user_id="me@example.com",
        # no triage_fn override -> real triage_thread runs against the fake client
    )

    assert len(result) == 1


def test_multiple_threads_mixed_triage_only_drafts_non_noise():
    graph = _FakeGraph()
    app = _fake_app_ctx(graph=graph)
    connector = _FakeConnector({"t1": _FakeThread("t1"), "t2": _FakeThread("t2")})
    gmail = _FakeGmail(["t1", "t2"])
    watch_state = _FakeWatchState(history_id="100")

    # First thread processed is noise, second is routine.
    call_count = {"n": 0}
    def _alternating_triage(client, summary):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return TriageResult(Priority.NOISE, "newsletter")
        return TriageResult(Priority.ROUTINE, "fine")

    result = handle_gmail_notification(
        app, {"emailAddress": "me@example.com", "historyId": "706"},
        gmail_service=gmail, watch_state=watch_state,
        connector=connector,
        post_approval=lambda *a: None,
        user_id="me@example.com",
        triage_fn=_alternating_triage,
    )

    assert len(result) == 1


# ---------------------------------------------------------------------------
# handle_calendar_notification
# ---------------------------------------------------------------------------

from aidedecamp.connectors.base import CalendarEvent
from aidedecamp.dispatcher import handle_calendar_notification


def _cal_event(event_id, start_offset_min, duration_min=30, summary="Meeting"):
    base = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    start = base + timedelta(minutes=start_offset_min)
    end = start + timedelta(minutes=duration_min)
    return CalendarEvent(event_id=event_id, summary=summary, start=start, end=end)


class _FakeCalendarConnector:
    """Minimal connector fake exposing only get_event/list_events, the two
    methods handle_calendar_notification's scheduling path uses."""

    def __init__(self, events_by_id: dict, nearby: list | None = None):
        self._events_by_id = events_by_id
        self._nearby = nearby if nearby is not None else list(events_by_id.values())

    def get_event(self, event_id):
        return self._events_by_id[event_id]

    def list_events(self, *, time_min, time_max):
        return self._nearby


class _FakeCalendarSyncState:
    def __init__(self, initial=None):
        self._data = dict(initial or {})

    def get(self, calendar_id):
        return self._data.get(calendar_id)

    def put(self, calendar_id, *, sync_token):
        self._data[calendar_id] = {"sync_token": sync_token}


class _FakeCalendarEventsService:
    def __init__(self, pages):
        self._pages = pages
        self._i = 0

    def events(self):
        svc = self

        class _Events:
            def list(self, **kwargs):
                class _Req:
                    def execute(self_):
                        page = svc._pages[svc._i]
                        svc._i += 1
                        return page
                return _Req()
        return _Events()


def test_calendar_notification_notifies_on_conflict():
    e1 = _cal_event("e1", 0, duration_min=60, summary="Client call")
    e2 = _cal_event("e2", 15, duration_min=30, summary="Standup")
    connector = _FakeCalendarConnector({"e1": e1, "e2": e2}, nearby=[e1, e2])
    sync_state = _FakeCalendarSyncState({"primary": {"sync_token": "old"}})
    calendar_service = _FakeCalendarEventsService(pages=[
        {"items": [{"id": "e1"}], "nextSyncToken": "new"}
    ])
    notifications = []

    result = handle_calendar_notification(
        _fake_app_ctx(), {"resource_state": "exists"},
        calendar_service=calendar_service,
        calendar_sync_state=sync_state,
        connector=connector,
        notify=notifications.append,
        user_id="me@example.com",
    )

    assert len(result) == 1
    assert result[0].conflicting_with.event_id == "e2"
    assert len(notifications) == 1
    assert "Client call" in notifications[0]
    assert "Standup" in notifications[0]


def test_calendar_notification_no_notify_when_no_conflict():
    e1 = _cal_event("e1", 0, duration_min=30)
    connector = _FakeCalendarConnector({"e1": e1}, nearby=[e1])
    sync_state = _FakeCalendarSyncState({"primary": {"sync_token": "old"}})
    calendar_service = _FakeCalendarEventsService(pages=[
        {"items": [{"id": "e1"}], "nextSyncToken": "new"}
    ])
    notifications = []

    result = handle_calendar_notification(
        _fake_app_ctx(), {"resource_state": "exists"},
        calendar_service=calendar_service,
        calendar_sync_state=sync_state,
        connector=connector,
        notify=notifications.append,
        user_id="me@example.com",
    )

    assert result == []
    assert notifications == []


def test_calendar_notification_skips_failed_event_fetch():
    connector = _FakeCalendarConnector({}, nearby=[])  # get_event raises KeyError
    sync_state = _FakeCalendarSyncState({"primary": {"sync_token": "old"}})
    calendar_service = _FakeCalendarEventsService(pages=[
        {"items": [{"id": "e1"}], "nextSyncToken": "new"}
    ])
    notifications = []

    result = handle_calendar_notification(
        _fake_app_ctx(), {"resource_state": "exists"},
        calendar_service=calendar_service,
        calendar_sync_state=sync_state,
        connector=connector,
        notify=notifications.append,
        user_id="me@example.com",
    )

    assert result == []
    assert notifications == []


def test_calendar_notification_full_syncs_on_expired():
    e1 = _cal_event("e1", 0)
    connector = _FakeCalendarConnector({"e1": e1}, nearby=[e1])
    sync_state = _FakeCalendarSyncState()  # no baseline -> SyncExpired
    calendar_service = _FakeCalendarEventsService(pages=[
        {"items": [{"id": "e1"}], "nextSyncToken": "fresh"}
    ])

    result = handle_calendar_notification(
        _fake_app_ctx(), {"resource_state": "sync"},
        calendar_service=calendar_service,
        calendar_sync_state=sync_state,
        connector=connector,
        notify=lambda text: None,
        user_id="me@example.com",
    )

    assert result == []  # no conflict, but no exception either
    assert sync_state.get("primary")["sync_token"] == "fresh"


def test_calendar_notification_records_audit_event():
    e1 = _cal_event("e1", 0, duration_min=60, summary="Client call")
    e2 = _cal_event("e2", 15, duration_min=30, summary="Standup")
    connector = _FakeCalendarConnector({"e1": e1, "e2": e2}, nearby=[e1, e2])
    sync_state = _FakeCalendarSyncState({"primary": {"sync_token": "old"}})
    calendar_service = _FakeCalendarEventsService(pages=[
        {"items": [{"id": "e1"}], "nextSyncToken": "new"}
    ])
    audit_log = _FakeAuditLog()

    handle_calendar_notification(
        _fake_app_ctx(), {"resource_state": "exists"},
        calendar_service=calendar_service,
        calendar_sync_state=sync_state,
        connector=connector,
        notify=lambda text: None,
        user_id="me@example.com",
        audit_log=audit_log,
    )

    assert len(audit_log.records) == 1
    rec = audit_log.records[0]
    assert rec["workflow"] == "scheduling"
    assert rec["domain"] == "calendar"
    assert rec["events"][0]["event"] == "conflict_detected"
    assert rec["events"][0]["conflicting_event_id"] == "e2"


def test_calendar_notification_no_audit_call_when_log_absent():
    e1 = _cal_event("e1", 0, duration_min=60)
    e2 = _cal_event("e2", 15, duration_min=30)
    connector = _FakeCalendarConnector({"e1": e1, "e2": e2}, nearby=[e1, e2])
    sync_state = _FakeCalendarSyncState({"primary": {"sync_token": "old"}})
    calendar_service = _FakeCalendarEventsService(pages=[
        {"items": [{"id": "e1"}], "nextSyncToken": "new"}
    ])

    # Must not raise even though no audit_log is provided.
    handle_calendar_notification(
        _fake_app_ctx(), {"resource_state": "exists"},
        calendar_service=calendar_service,
        calendar_sync_state=sync_state,
        connector=connector,
        notify=lambda text: None,
        user_id="me@example.com",
    )


# ---------------------------------------------------------------------------
# handle_chat_interaction — the async half of Chat's approve/reject flow
# ---------------------------------------------------------------------------


def _click(fn: str, thread_id: str = "t-1") -> dict:
    return {
        "type": "CARD_CLICKED",
        "action": {
            "actionMethodName": fn,
            "parameters": [{"key": "thread_id", "value": thread_id}],
        },
    }


def test_chat_interaction_approve_resumes_and_posts_confirmation():
    resumes = []
    replies = []

    handle_chat_interaction(
        _fake_app_ctx(),
        _click("adc_approve", "t-42"),
        resume_fn=lambda tid, decision, text: resumes.append((tid, decision, text)),
        post_text=replies.append,
        user_id="me@example.com",
    )

    assert resumes == [("t-42", "approved", None)]
    assert "Approved" in replies[0]


def test_chat_interaction_reject_resumes_and_posts_confirmation():
    resumes = []
    replies = []

    handle_chat_interaction(
        _fake_app_ctx(),
        _click("adc_reject", "t-9"),
        resume_fn=lambda tid, decision, text: resumes.append((tid, decision, text)),
        post_text=replies.append,
        user_id="me@example.com",
    )

    assert resumes == [("t-9", "rejected", None)]
    assert "Rejected" in replies[0]


def test_chat_interaction_edit_submit_resumes_with_text():
    """The edit dialog's submit rides the same async path as approve/reject
    (prompt 02) — resumed as 'edited' with the user's text, so
    capture_correction fires in the graph."""
    resumes = []
    replies = []

    event = _click("adc_edit_submit", "t-8")
    event["common"] = {
        "formInputs": {"adc_edit_text": {"stringInputs": {"value": ["Rewritten."]}}}
    }
    handle_chat_interaction(
        _fake_app_ctx(),
        event,
        resume_fn=lambda tid, decision, text: resumes.append((tid, decision, text))
        or {"applied_ref": "d-4"},
        post_text=replies.append,
        user_id="me@example.com",
    )

    assert resumes == [("t-8", "edited", "Rewritten.")]
    assert replies == ["✏️ Edited — draft created in Gmail."]


def test_chat_interaction_confirmation_reports_created_draft():
    """When the resumed graph materialized a Gmail draft (applied_ref set),
    the confirmation says so — and only then (prompt 01: honesty)."""
    replies = []

    handle_chat_interaction(
        _fake_app_ctx(),
        _click("adc_approve", "t-42"),
        resume_fn=lambda tid, decision, text: {"applied_ref": "d-7"},
        post_text=replies.append,
        user_id="me@example.com",
    )

    assert replies == ["✅ Approved — draft created in Gmail."]


def test_chat_interaction_confirmation_admits_apply_failure():
    replies = []

    handle_chat_interaction(
        _fake_app_ctx(),
        _click("adc_approve", "t-42"),
        resume_fn=lambda tid, decision, text: {"apply_error": "ConnectionError"},
        post_text=replies.append,
        user_id="me@example.com",
    )

    assert "failed" in replies[0] and "ConnectionError" in replies[0]


def test_chat_interaction_edit_ignored():
    """Edit's initial click never touches the graph — handled synchronously
    by the republisher, so it must never reach this async path at all."""
    resumes = []
    replies = []

    handle_chat_interaction(
        _fake_app_ctx(),
        _click("adc_edit", "t-1"),
        resume_fn=lambda tid, decision, text: resumes.append((tid, decision, text)),
        post_text=replies.append,
        user_id="me@example.com",
    )

    assert resumes == []
    assert replies == []


def test_chat_interaction_unknown_action_ignored():
    resumes = []
    replies = []

    handle_chat_interaction(
        _fake_app_ctx(),
        _click("unknown_fn", "t-1"),
        resume_fn=lambda tid, decision, text: resumes.append((tid, decision, text)),
        post_text=replies.append,
        user_id="me@example.com",
    )

    assert resumes == []
    assert replies == []


def test_chat_interaction_missing_thread_id_ignored():
    resumes = []
    event = {
        "type": "CARD_CLICKED",
        "action": {"actionMethodName": "adc_approve", "parameters": []},
    }

    handle_chat_interaction(
        _fake_app_ctx(),
        event,
        resume_fn=lambda tid, decision, text: resumes.append((tid, decision, text)),
        post_text=lambda text: None,
        user_id="me@example.com",
    )

    assert resumes == []


def test_chat_interaction_records_audit_event():
    audit_log = _FakeAuditLog()

    handle_chat_interaction(
        _fake_app_ctx(),
        _click("adc_approve", "t-42"),
        resume_fn=lambda tid, decision, text: None,
        post_text=lambda text: None,
        user_id="me@example.com",
        audit_log=audit_log,
    )

    assert len(audit_log.records) == 1
    rec = audit_log.records[0]
    assert rec["thread_id"] == "t-42"
    assert rec["workflow"] == "draft_approve"
    assert rec["domain"] == "chat"
    assert rec["events"][0]["event"] == "chat_interaction_resumed"
    assert rec["events"][0]["decision"] == "approved"


def test_chat_interaction_no_audit_call_when_log_absent():
    # Must not raise even though no audit_log is provided.
    handle_chat_interaction(
        _fake_app_ctx(),
        _click("adc_reject", "t-1"),
        resume_fn=lambda tid, decision, text: None,
        post_text=lambda text: None,
        user_id="me@example.com",
    )


# ---------------------------------------------------------------------------
# handle_chat_message
# ---------------------------------------------------------------------------

def _chat_event(text: str = "hello") -> dict:
    return {
        "type": "google.workspace.chat.message.v1.created",
        "message": {
            "text": text,
            "argumentText": text,
            "sender": {"name": "users/U1", "type": "HUMAN"},
            "space": {"name": "spaces/ABC"},
        },
    }


def test_chat_message_converses_for_regular_text():
    client = _FakeClient(reply="here is your answer")
    app = _fake_app_ctx(client=client)
    replies = []

    handle_chat_message(app, _chat_event("what's on my calendar?"),
                        post_text=replies.append, user_id="me@example.com")

    assert len(replies) == 1
    assert replies[0] == "here is your answer"


def test_chat_message_calls_brief_fn_for_brief_keyword():
    app = _fake_app_ctx()
    replies = []

    handle_chat_message(
        app, _chat_event("please send me the morning brief"),
        post_text=replies.append,
        user_id="me@example.com",
        brief_fn=lambda: "brief content here",
    )

    assert replies == ["brief content here"]


def test_chat_message_brief_fn_triggered_by_summary_keyword():
    app = _fake_app_ctx()
    replies = []
    handle_chat_message(
        app, _chat_event("I need a summary"),
        post_text=replies.append,
        user_id="me@example.com",
        brief_fn=lambda: "your summary",
    )
    assert replies == ["your summary"]


def test_chat_message_bot_event_ignored():
    app = _fake_app_ctx()
    replies = []
    bot_event = {
        "type": "google.workspace.chat.message.v1.created",
        "message": {
            "text": "bot message",
            "argumentText": "bot message",
            "sender": {"name": "bots/B1", "type": "BOT"},
            "space": {"name": "spaces/ABC"},
        },
    }
    handle_chat_message(app, bot_event, post_text=replies.append, user_id="me@example.com")
    assert replies == []


def test_chat_message_non_message_event_ignored():
    app = _fake_app_ctx()
    replies = []
    handle_chat_message(
        app, {"type": "ADDED_TO_SPACE"},
        post_text=replies.append, user_id="me@example.com"
    )
    assert replies == []


def test_chat_no_brief_fn_returns_fallback():
    app = _fake_app_ctx()
    replies = []
    handle_chat_message(
        app, _chat_event("morning brief please"),
        post_text=replies.append,
        user_id="me@example.com",
        # no brief_fn
    )
    assert "Brief not configured" in replies[0]


# ---------------------------------------------------------------------------
# handle_slack_message — same brief/converse routing, no event decoding step
# ---------------------------------------------------------------------------


def test_slack_message_converses_for_regular_text():
    client = _FakeClient(reply="here is your answer")
    app = _fake_app_ctx(client=client)
    replies = []

    handle_slack_message(
        app, text="what's on my calendar?", user_id="U1", post_text=replies.append
    )

    assert replies == ["here is your answer"]


def test_slack_message_calls_brief_fn_for_brief_keyword():
    app = _fake_app_ctx()
    replies = []

    handle_slack_message(
        app, text="give me the morning brief", user_id="U1",
        post_text=replies.append, brief_fn=lambda: "brief content here",
    )

    assert replies == ["brief content here"]


def test_slack_message_brief_fn_triggered_by_summary_keyword():
    app = _fake_app_ctx()
    replies = []

    handle_slack_message(
        app, text="I need a summary", user_id="U1",
        post_text=replies.append, brief_fn=lambda: "your summary",
    )

    assert replies == ["your summary"]


def test_slack_message_no_brief_fn_returns_fallback():
    app = _fake_app_ctx()
    replies = []

    handle_slack_message(
        app, text="morning brief please", user_id="U1", post_text=replies.append,
        # no brief_fn
    )

    assert "Brief not configured" in replies[0]


def test_slack_message_and_chat_message_share_routing_logic():
    """handle_slack_message and handle_chat_message must agree on which
    keywords trigger the brief path, since they share _respond_to_message."""
    app = _fake_app_ctx()
    slack_replies = []
    chat_replies = []

    handle_slack_message(
        app, text="summary please", user_id="U1",
        post_text=slack_replies.append, brief_fn=lambda: "B",
    )
    handle_chat_message(
        app, _chat_event("summary please"),
        post_text=chat_replies.append, user_id="me@example.com", brief_fn=lambda: "B",
    )

    assert slack_replies == chat_replies == ["B"]


# ---------------------------------------------------------------------------
# _converse
# ---------------------------------------------------------------------------

class _FakeMemResult:
    def __init__(self, text):
        self.text = text


def test_converse_includes_memory_in_prompt():
    store = _FakeMemoryStore(results=[_FakeMemResult("user prefers short replies")])
    client = _FakeClient(reply="got it")
    app = _fake_app_ctx(store=store, client=client)

    result = _converse(app, "help me", user_id="me@example.com")

    assert result == "got it"
    # The memory snippet must appear in the system prompt
    call = client.calls[0]
    system = call["messages"][0]["content"]
    assert "user prefers short replies" in system


def test_converse_tags_input_as_untrusted():
    client = _FakeClient(reply="ok")
    app = _fake_app_ctx(client=client)

    _converse(app, "tell me something", user_id="me@example.com")

    user_msg = client.calls[0]["messages"][1]["content"]
    assert "UNTRUSTED" in user_msg


def test_converse_uses_converse_model():
    from aidedecamp.fuelix import Task, model_for
    client = _FakeClient(reply="ok")
    app = _fake_app_ctx(client=client)

    _converse(app, "hi", user_id="me@example.com")

    assert client.calls[0]["model"] == model_for(Task.CONVERSE)
