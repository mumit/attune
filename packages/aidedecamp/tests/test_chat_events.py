"""Tests for ingestion/chat_events.py — no live Google connection needed.

workspace_events and SubscriptionState are injected fakes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aidedecamp.ingestion.chat_events import (
    RENEW_WHEN_HOURS_LEFT,
    ChatMessage,
    SubscriptionResult,
    ensure_subscription,
    process_chat_event,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


class _FakeState:
    """In-memory SubscriptionState for tests."""

    def __init__(self, initial: dict | None = None):
        self._store: dict = dict(initial or {})

    def get(self, space: str):
        return self._store.get(space)

    def put(self, space: str, *, subscription_name: str, expiration: datetime) -> None:
        self._store[space] = {"subscription_name": subscription_name, "expiration": expiration}


def _fake_workspace_events(*, name: str = "subscriptions/sub-1", expire_hours: int = 168):
    """Return a fake workspaceevents service that records create calls."""
    calls = []
    expire_time = (_now() + timedelta(hours=expire_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    class _Req:
        def execute(self):
            return {"name": name, "expireTime": expire_time}

    class _Subs:
        def create(self, body):
            calls.append(body)
            return _Req()

    class _Service:
        def subscriptions(self):
            return _Subs()

    return _Service(), calls


# ---------------------------------------------------------------------------
# ensure_subscription — creation
# ---------------------------------------------------------------------------

def test_creates_subscription_when_no_state():
    svc, calls = _fake_workspace_events()
    state = _FakeState()

    result = ensure_subscription(
        svc, state, space="spaces/ABC", topic="projects/p/topics/t"
    )

    assert result.renewed is True
    assert result.subscription_name == "subscriptions/sub-1"
    assert len(calls) == 1
    assert calls[0]["targetResource"] == "//chat.googleapis.com/spaces/ABC"
    assert calls[0]["notificationEndpoint"]["pubsubTopic"] == "projects/p/topics/t"


def test_state_updated_after_creation():
    svc, _ = _fake_workspace_events()
    state = _FakeState()

    ensure_subscription(svc, state, space="spaces/ABC", topic="projects/p/topics/t")
    stored = state.get("spaces/ABC")

    assert stored is not None
    assert stored["subscription_name"] == "subscriptions/sub-1"
    assert isinstance(stored["expiration"], datetime)


# ---------------------------------------------------------------------------
# ensure_subscription — skip when healthy
# ---------------------------------------------------------------------------

def test_skips_renewal_when_far_from_expiry():
    svc, calls = _fake_workspace_events()
    state = _FakeState({
        "spaces/ABC": {
            "subscription_name": "subscriptions/old",
            "expiration": _now() + timedelta(hours=RENEW_WHEN_HOURS_LEFT + 24),
        }
    })

    result = ensure_subscription(svc, state, space="spaces/ABC", topic="projects/p/topics/t")

    assert result.renewed is False
    assert result.subscription_name == "subscriptions/old"
    assert calls == []


def test_renews_when_near_expiry():
    svc, calls = _fake_workspace_events(name="subscriptions/new")
    state = _FakeState({
        "spaces/ABC": {
            "subscription_name": "subscriptions/old",
            # just inside the renew window
            "expiration": _now() + timedelta(hours=RENEW_WHEN_HOURS_LEFT - 1),
        }
    })

    result = ensure_subscription(svc, state, space="spaces/ABC", topic="projects/p/topics/t")

    assert result.renewed is True
    assert result.subscription_name == "subscriptions/new"
    assert len(calls) == 1


def test_force_renews_even_when_healthy():
    svc, calls = _fake_workspace_events(name="subscriptions/forced")
    state = _FakeState({
        "spaces/ABC": {
            "subscription_name": "subscriptions/old",
            "expiration": _now() + timedelta(days=6),
        }
    })

    result = ensure_subscription(
        svc, state, space="spaces/ABC", topic="projects/p/topics/t", force=True
    )

    assert result.renewed is True
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# ensure_subscription — string expiration in state
# ---------------------------------------------------------------------------

def test_parses_string_expiration_in_state():
    svc, calls = _fake_workspace_events()
    exp_str = (_now() + timedelta(days=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = _FakeState({
        "spaces/ABC": {
            "subscription_name": "subscriptions/old",
            "expiration": exp_str,
        }
    })

    result = ensure_subscription(svc, state, space="spaces/ABC", topic="projects/p/topics/t")

    # healthy expiry → should NOT renew
    assert result.renewed is False
    assert calls == []


def test_fallback_expiry_when_api_omits_expire_time():
    """If the API response has no expireTime, default to 7 days from now."""

    class _NoExpSvc:
        def subscriptions(self):
            class _S:
                def create(self, body):
                    class _R:
                        def execute(self):
                            return {"name": "subscriptions/x"}
                    return _R()
            return _S()

    state = _FakeState()
    result = ensure_subscription(_NoExpSvc(), state, space="spaces/X", topic="t")
    assert result.renewed is True
    assert result.expiration > _now() + timedelta(days=6)


# ---------------------------------------------------------------------------
# process_chat_event — message parsing
# ---------------------------------------------------------------------------

def _msg_event(text: str = "hello", sender_type: str = "HUMAN", *, space: str = "spaces/ABC") -> dict:
    return {
        "type": "google.workspace.chat.message.v1.created",
        "message": {
            "name": f"{space}/messages/m1",
            "text": text,
            "argumentText": text,
            "sender": {"name": "users/U1", "type": sender_type},
            "space": {"name": space},
        },
    }


def test_parses_human_message():
    msg = process_chat_event(_msg_event("please send brief"))
    assert isinstance(msg, ChatMessage)
    assert msg.text == "please send brief"
    assert msg.space == "spaces/ABC"
    assert msg.sender == "users/U1"


def test_bot_message_returns_none():
    msg = process_chat_event(_msg_event("bot reply", sender_type="BOT"))
    assert msg is None


def test_non_message_event_returns_none():
    msg = process_chat_event({"type": "google.workspace.chat.membership.v1.created"})
    assert msg is None


def test_argument_text_preferred_over_text():
    event = {
        "type": "google.workspace.chat.message.v1.created",
        "message": {
            "text": "@bot hello world",
            "argumentText": "hello world",
            "sender": {"name": "users/U1", "type": "HUMAN"},
            "space": {"name": "spaces/ABC"},
        },
    }
    msg = process_chat_event(event)
    assert msg.text == "hello world"


def test_falls_back_to_text_when_no_argument_text():
    event = {
        "type": "google.workspace.chat.message.v1.created",
        "message": {
            "text": "plain message",
            "sender": {"name": "users/U1", "type": "HUMAN"},
            "space": {"name": "spaces/ABC"},
        },
    }
    msg = process_chat_event(event)
    assert msg.text == "plain message"


def test_space_from_event_top_level_fallback():
    event = {
        "type": "google.workspace.chat.message.v1.created",
        "space": {"name": "spaces/XYZ"},
        "message": {
            "text": "hi",
            "argumentText": "hi",
            "sender": {"name": "users/U1", "type": "HUMAN"},
        },
    }
    msg = process_chat_event(event)
    assert msg.space == "spaces/XYZ"


def test_event_type_in_eventType_field():
    event = {
        "eventType": "google.workspace.chat.message.v1.created",
        "message": {
            "text": "hi",
            "argumentText": "hi",
            "sender": {"name": "users/U1", "type": "HUMAN"},
            "space": {"name": "spaces/A"},
        },
    }
    msg = process_chat_event(event)
    assert msg is not None


def test_message_name_captured():
    msg = process_chat_event(_msg_event("hello"))
    assert msg.message_name == "spaces/ABC/messages/m1"
