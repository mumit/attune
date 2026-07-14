"""Tests for ingestion/polling.py + Runtime.poll_once (roadmap prompt 09).
All offline: fake Gmail/Chat services, injected state."""

from __future__ import annotations

from attune.ingestion.polling import (
    calendar_poll_notification,
    poll_chat_step,
    poll_gmail_step,
)


# ---------------------------------------------------------------------------
# poll_gmail_step
# ---------------------------------------------------------------------------


class _FakeGmailProfile:
    def __init__(self, history_id="500"):
        self._history_id = history_id

    def users(self):
        hid = self._history_id

        class _Users:
            def getProfile(self, userId):
                class _Req:
                    def execute(self_):
                        return {"emailAddress": userId, "historyId": hid}
                return _Req()

        return _Users()


class _DictWatchState:
    def __init__(self, data=None):
        self.data = data or {}
        self.puts: list = []

    def get(self, email):
        return self.data.get(email)

    def put(self, email, *, history_id, expiration):
        self.data[email] = {"history_id": history_id, "expiration": expiration}
        self.puts.append(history_id)


def test_gmail_first_run_baselines_without_notification():
    """No baseline -> store current historyId, return None: start from now,
    never replay the mailbox."""
    state = _DictWatchState()
    result = poll_gmail_step(_FakeGmailProfile("500"), state, email="me")

    assert result is None
    assert state.data["me"]["history_id"] == "500"


def test_gmail_synthesizes_notification_only_when_advanced():
    state = _DictWatchState({"me": {"history_id": "400", "expiration": None}})
    result = poll_gmail_step(_FakeGmailProfile("500"), state, email="me")
    assert result == {"emailAddress": "me", "historyId": "500"}

    # same id -> idle
    state = _DictWatchState({"me": {"history_id": "500", "expiration": None}})
    assert poll_gmail_step(_FakeGmailProfile("500"), state, email="me") is None


def test_gmail_step_does_not_advance_baseline_itself():
    """The baseline advances inside process_notification (after a successful
    reconcile), not here — a synthesized notification that fails to process
    must be re-synthesized next tick."""
    state = _DictWatchState({"me": {"history_id": "400", "expiration": None}})
    poll_gmail_step(_FakeGmailProfile("500"), state, email="me")
    assert state.data["me"]["history_id"] == "400"


# ---------------------------------------------------------------------------
# poll_chat_step
# ---------------------------------------------------------------------------


class _FakeChatService:
    def __init__(self, messages=None):
        self._messages = messages or []
        self.list_calls: list[dict] = []

    def spaces(self):
        outer = self

        class _Messages:
            def list(self, **kwargs):
                outer.list_calls.append(kwargs)

                class _Req:
                    def execute(self_):
                        return {"messages": outer._messages}
                return _Req()

        class _Spaces:
            def messages(self):
                return _Messages()

        return _Spaces()


def test_chat_first_run_returns_now_mark_and_no_events():
    events, mark = poll_chat_step(
        _FakeChatService(), space="spaces/A", last_seen=None
    )
    assert events == []
    assert mark is not None  # "now" — start from here


def test_chat_synthesizes_workspace_event_shape():
    msg = {
        "name": "spaces/A/messages/m1",
        "text": "hello",
        "createTime": "2026-07-10T12:00:00Z",
        "sender": {"name": "users/1", "type": "HUMAN"},
        "space": {"name": "spaces/A"},
    }
    service = _FakeChatService([msg])
    events, mark = poll_chat_step(
        service, space="spaces/A", last_seen="2026-07-10T11:00:00Z"
    )

    assert mark == "2026-07-10T12:00:00Z"
    assert len(events) == 1
    # decodes through the same path push mode uses
    from attune.ingestion import process_chat_event

    chat_msg = process_chat_event(events[0])
    assert chat_msg is not None
    assert chat_msg.text == "hello"
    assert chat_msg.space == "spaces/A"
    # and the query used the high-water mark
    assert 'createTime > "2026-07-10T11:00:00Z"' in service.list_calls[0]["filter"]


def test_chat_no_new_messages_keeps_mark():
    events, mark = poll_chat_step(
        _FakeChatService([]), space="spaces/A", last_seen="2026-07-10T11:00:00Z"
    )
    assert events == []
    assert mark is None  # caller keeps the existing mark


def test_event_type_constant_matches_chat_events():
    from attune.ingestion.chat_events import _MESSAGE_CREATED as REAL
    from attune.ingestion.polling import _MESSAGE_CREATED as MIRRORED

    assert MIRRORED == REAL


def test_calendar_poll_notification_shape():
    assert calendar_poll_notification() == {"resource_state": "poll"}
