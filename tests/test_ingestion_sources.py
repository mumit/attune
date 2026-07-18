"""Tests for ingestion/sources.py — Slack/Chat attended sources (Phase 2
stage 1, docs/future-state.md; gaps G1/G3). All offline: fake Slack/Chat
clients, injected state and retry queue."""

from __future__ import annotations

from datetime import datetime, timezone

from attune.ingestion.sources import (
    TEXT_CHAR_CAP,
    SourceMessage,
    chat_message_to_source,
    poll_chat_source,
    poll_slack_source,
    slack_message_to_source,
)


# ---------------------------------------------------------------------------
# SourceMessage
# ---------------------------------------------------------------------------


def test_source_message_truncates_text_to_cap():
    msg = SourceMessage(
        source="slack", channel_ref="C1", channel_name="C1",
        sender_ref="U1", sender_display="U1", text="x" * (TEXT_CHAR_CAP + 500),
        ts=datetime.now(timezone.utc), thread_ref=None, mentions_principal=False,
    )
    assert len(msg.text) == TEXT_CHAR_CAP


def test_source_message_short_text_is_unchanged():
    msg = SourceMessage(
        source="slack", channel_ref="C1", channel_name="C1",
        sender_ref="U1", sender_display="U1", text="hello",
        ts=datetime.now(timezone.utc), thread_ref=None, mentions_principal=False,
    )
    assert msg.text == "hello"


# ---------------------------------------------------------------------------
# Mention detection
# ---------------------------------------------------------------------------


def test_slack_mention_detected_against_allowlist():
    msg = slack_message_to_source(
        {"user": "U2", "text": "hey <@U1> can you look", "ts": "1717000000.000100"},
        channel_id="C1", principal_member_ids=frozenset({"U1"}),
    )
    assert msg.mentions_principal is True


def test_slack_no_mention_when_id_not_present():
    msg = slack_message_to_source(
        {"user": "U2", "text": "hey <@U9> can you look", "ts": "1717000000.000100"},
        channel_id="C1", principal_member_ids=frozenset({"U1"}),
    )
    assert msg.mentions_principal is False


def test_chat_mention_detected_via_user_mention_annotation():
    message = {
        "sender": {"name": "users/2", "type": "HUMAN"},
        "text": "hey @you",
        "createTime": "2026-07-10T12:00:00Z",
        "annotations": [
            {
                "type": "USER_MENTION",
                "userMention": {"user": {"name": "users/1", "type": "HUMAN"}},
            }
        ],
    }
    msg = chat_message_to_source(
        message, space="spaces/A", principal_member_ids=frozenset({"users/1"})
    )
    assert msg.mentions_principal is True


def test_chat_no_mention_without_matching_annotation():
    message = {
        "sender": {"name": "users/2", "type": "HUMAN"},
        "text": "hey there",
        "createTime": "2026-07-10T12:00:00Z",
    }
    msg = chat_message_to_source(
        message, space="spaces/A", principal_member_ids=frozenset({"users/1"})
    )
    assert msg.mentions_principal is False


# ---------------------------------------------------------------------------
# Slack: cursor discipline, bot skip, bounded cap
# ---------------------------------------------------------------------------


class _DictSourceState:
    def __init__(self, data=None):
        self.data = data or {}
        self.puts: list = []

    def get(self, key):
        return self.data.get(key)

    def put(self, key, *, last_seen):
        self.data[key] = {"last_seen": last_seen}
        self.puts.append((key, last_seen))


class _FakeSlackClient:
    def __init__(self, messages):
        self._messages = messages
        self.calls: list[dict] = []

    def conversations_history(self, **kwargs):
        self.calls.append(kwargs)
        return {"messages": self._messages}


class _FakeRetryQueue:
    def __init__(self):
        self.enqueued: list[tuple] = []

    def enqueue(self, kind, source_ref, payload, *, error):
        self.enqueued.append((kind, source_ref, payload, error))


def test_slack_first_run_baselines_without_dispatch():
    state = _DictSourceState()
    dispatched = []
    count = poll_slack_source(
        _FakeSlackClient([]), "C1", state, dispatch=dispatched.append,
    )
    assert count == 0
    assert dispatched == []
    assert state.get("slack:C1")["last_seen"] is not None


def test_slack_dispatches_new_messages_oldest_first():
    # Slack returns newest-first.
    messages = [
        {"user": "U2", "text": "second", "ts": "200.0"},
        {"user": "U2", "text": "first", "ts": "100.0"},
    ]
    state = _DictSourceState({"slack:C1": {"last_seen": "50.0"}})
    dispatched = []
    count = poll_slack_source(
        _FakeSlackClient(messages), "C1", state, dispatch=dispatched.append,
    )
    assert count == 2
    assert [m.text for m in dispatched] == ["first", "second"]
    assert state.get("slack:C1")["last_seen"] == "200.0"


def test_slack_skips_bot_messages():
    # Slack returns newest-first.
    messages = [
        {"user": "BOTUSER", "text": "also bot by id", "ts": "102.0"},
        {"bot_id": "B1", "text": "bot", "ts": "101.0"},
        {"user": "U2", "text": "human", "ts": "100.0"},
    ]
    state = _DictSourceState({"slack:C1": {"last_seen": "50.0"}})
    dispatched = []
    count = poll_slack_source(
        _FakeSlackClient(messages), "C1", state, dispatch=dispatched.append,
        bot_user_id="BOTUSER",
    )
    assert count == 1
    assert dispatched[0].text == "human"
    # The cursor still advances past all listed messages, bot or not.
    assert state.get("slack:C1")["last_seen"] == "102.0"


def test_slack_bounds_messages_per_poll():
    messages = [{"user": "U2", "text": str(i), "ts": f"{100 + i}.0"} for i in range(10)]
    state = _DictSourceState({"slack:C1": {"last_seen": "50.0"}})
    dispatched = []
    count = poll_slack_source(
        _FakeSlackClient(messages), "C1", state, dispatch=dispatched.append,
        max_messages=3,
    )
    assert count == 3


def test_slack_client_receives_bounded_limit_and_oldest_cursor():
    client = _FakeSlackClient([])
    state = _DictSourceState({"slack:C1": {"last_seen": "50.0"}})
    poll_slack_source(client, "C1", state, dispatch=lambda m: None, max_messages=7)
    assert client.calls[0]["oldest"] == "50.0"
    assert client.calls[0]["limit"] == 7


def test_slack_dispatch_failure_enqueues_retry_and_cursor_still_advances():
    """The cursor advances only after durable handling OR durable retry
    recording — a failing dispatch must not lose the message nor block the
    next poll from advancing past it."""
    # Slack returns newest-first.
    messages = [
        {"user": "U2", "text": "boom", "ts": "101.0"},
        {"user": "U2", "text": "ok", "ts": "100.0"},
    ]
    state = _DictSourceState({"slack:C1": {"last_seen": "50.0"}})
    retry_queue = _FakeRetryQueue()
    dispatched = []

    def _dispatch(message):
        if message.text == "boom":
            raise RuntimeError("kaboom")
        dispatched.append(message)

    count = poll_slack_source(
        _FakeSlackClient(messages), "C1", state, dispatch=_dispatch,
        retry_queue=retry_queue,
    )

    assert count == 2
    assert [m.text for m in dispatched] == ["ok"]
    assert len(retry_queue.enqueued) == 1
    kind, source_ref, payload, error = retry_queue.enqueued[0]
    assert kind == "slack_source"
    assert source_ref == "C1:101.0"
    assert payload["raw"]["text"] == "boom"
    assert error == "RuntimeError"
    # The message is durably queued -> the cursor is safe to advance past it.
    assert state.get("slack:C1")["last_seen"] == "101.0"


def test_slack_dispatch_failure_without_retry_queue_raises():
    messages = [{"user": "U2", "text": "boom", "ts": "100.0"}]
    state = _DictSourceState({"slack:C1": {"last_seen": "50.0"}})

    def _dispatch(message):
        raise RuntimeError("kaboom")

    try:
        poll_slack_source(
            _FakeSlackClient(messages), "C1", state, dispatch=_dispatch,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected the failure to propagate without a retry queue")


# ---------------------------------------------------------------------------
# Chat: cursor discipline, bot skip, bounded cap
# ---------------------------------------------------------------------------


class _FakeChatService:
    def __init__(self, messages):
        self._messages = messages
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


def test_chat_first_run_baselines_without_dispatch():
    state = _DictSourceState()
    dispatched = []
    count = poll_chat_source(
        _FakeChatService([]), "spaces/A", state, dispatch=dispatched.append,
    )
    assert count == 0
    assert dispatched == []
    assert state.get("chat:spaces/A")["last_seen"] is not None


def test_chat_dispatches_new_messages():
    messages = [
        {
            "sender": {"name": "users/2", "type": "HUMAN"},
            "text": "hi",
            "createTime": "2026-07-10T12:00:01Z",
        },
    ]
    state = _DictSourceState({"chat:spaces/A": {"last_seen": "2026-07-10T12:00:00Z"}})
    dispatched = []
    count = poll_chat_source(
        _FakeChatService(messages), "spaces/A", state, dispatch=dispatched.append,
    )
    assert count == 1
    assert dispatched[0].text == "hi"
    assert state.get("chat:spaces/A")["last_seen"] == "2026-07-10T12:00:01Z"


def test_chat_skips_bot_messages():
    messages = [
        {
            "sender": {"name": "users/2", "type": "HUMAN"},
            "text": "human",
            "createTime": "2026-07-10T12:00:01Z",
        },
        {
            "sender": {"name": "users/bot", "type": "BOT"},
            "text": "bot",
            "createTime": "2026-07-10T12:00:02Z",
        },
    ]
    state = _DictSourceState({"chat:spaces/A": {"last_seen": "2026-07-10T12:00:00Z"}})
    dispatched = []
    count = poll_chat_source(
        _FakeChatService(messages), "spaces/A", state, dispatch=dispatched.append,
    )
    assert count == 1
    assert dispatched[0].text == "human"
    assert state.get("chat:spaces/A")["last_seen"] == "2026-07-10T12:00:02Z"


def test_chat_dispatch_failure_enqueues_retry_and_cursor_still_advances():
    messages = [
        {
            "sender": {"name": "users/2", "type": "HUMAN"},
            "text": "boom",
            "createTime": "2026-07-10T12:00:01Z",
            "name": "spaces/A/messages/m1",
        },
    ]
    state = _DictSourceState({"chat:spaces/A": {"last_seen": "2026-07-10T12:00:00Z"}})
    retry_queue = _FakeRetryQueue()

    def _dispatch(message):
        raise RuntimeError("kaboom")

    count = poll_chat_source(
        _FakeChatService(messages), "spaces/A", state, dispatch=_dispatch,
        retry_queue=retry_queue,
    )

    assert count == 1
    assert len(retry_queue.enqueued) == 1
    kind, source_ref, payload, error = retry_queue.enqueued[0]
    assert kind == "chat_source"
    assert source_ref == "spaces/A:spaces/A/messages/m1"
    assert payload["message"]["text"] == "boom"
    assert state.get("chat:spaces/A")["last_seen"] == "2026-07-10T12:00:01Z"


def test_slack_and_chat_source_keys_never_collide_in_shared_state():
    state = _DictSourceState()
    poll_slack_source(_FakeSlackClient([]), "spaces/A", state, dispatch=lambda m: None)
    poll_chat_source(_FakeChatService([]), "spaces/A", state, dispatch=lambda m: None)
    assert set(state.data.keys()) == {"slack:spaces/A", "chat:spaces/A"}
