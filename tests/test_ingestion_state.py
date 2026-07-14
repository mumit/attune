"""Tests for ingestion/state.py — JSON-file-backed WatchState/SubscriptionState.

No live services; everything is a tmp_path file. The critical thing under test
is that each class serializes ``expiration`` the way its *consuming* module's
read path expects (epoch-ms for Gmail, ISO-8601 for Chat) — get that backwards
and renewal silently mistimes itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from attune.ingestion.chat_events import RENEW_WHEN_HOURS_LEFT as CHAT_RENEW_HOURS
from attune.ingestion.chat_events import ensure_subscription
from attune.ingestion.gmail_watch import RENEW_WHEN_HOURS_LEFT as GMAIL_RENEW_HOURS
from attune.ingestion.gmail_watch import ensure_watch
from attune.ingestion.state import JsonChatSubscriptionState, JsonGmailWatchState


# ---------------------------------------------------------------------------
# JsonGmailWatchState — basic persistence
# ---------------------------------------------------------------------------


def test_gmail_watch_state_roundtrips_history_id(tmp_path):
    path = tmp_path / "watch.json"
    state = JsonGmailWatchState(str(path))
    exp = datetime.now(timezone.utc) + timedelta(days=7)

    state.put("me@example.com", history_id="100", expiration=exp)
    stored = state.get("me@example.com")

    assert stored["history_id"] == "100"


def test_gmail_watch_state_missing_key_returns_none(tmp_path):
    state = JsonGmailWatchState(str(tmp_path / "watch.json"))
    assert state.get("nobody@example.com") is None


def test_gmail_watch_state_persists_across_instances(tmp_path):
    path = tmp_path / "watch.json"
    exp = datetime.now(timezone.utc) + timedelta(days=7)
    JsonGmailWatchState(str(path)).put("a@b.com", history_id="1", expiration=exp)

    reloaded = JsonGmailWatchState(str(path))
    assert reloaded.get("a@b.com")["history_id"] == "1"


def test_gmail_watch_state_can_preserve_a_reloaded_expiration(tmp_path):
    """Polling advances historyId after reloading epoch-ms state from disk."""
    path = tmp_path / "watch.json"
    state = JsonGmailWatchState(str(path))
    exp = datetime.now(timezone.utc) + timedelta(days=7)
    state.put("me", history_id="100", expiration=exp)

    persisted_expiration = state.get("me")["expiration"]
    state.put("me", history_id="200", expiration=persisted_expiration)

    assert state.get("me") == {
        "history_id": "200",
        "expiration": persisted_expiration,
    }


def test_gmail_watch_state_creates_parent_dirs(tmp_path):
    path = tmp_path / "nested" / "watch.json"
    state = JsonGmailWatchState(str(path))
    exp = datetime.now(timezone.utc) + timedelta(days=7)
    state.put("a@b.com", history_id="1", expiration=exp)
    assert path.exists()


# ---------------------------------------------------------------------------
# JsonGmailWatchState — round-trips correctly through ensure_watch's own
# expiration-parsing path (the thing that actually matters)
# ---------------------------------------------------------------------------


class _FakeGmailService:
    def __init__(self, history_id="999", expiration_ms=None):
        self._history_id = history_id
        self._expiration_ms = expiration_ms

    def users(self):
        svc = self

        class _Users:
            def watch(self, userId, body):
                class _Req:
                    def execute(self_):
                        return {
                            "historyId": svc._history_id,
                            "expiration": str(svc._expiration_ms),
                        }
                return _Req()
        return _Users()


def test_gmail_watch_state_skips_renewal_when_fresh(tmp_path):
    path = tmp_path / "watch.json"
    state = JsonGmailWatchState(str(path))
    # Seed a healthy, far-from-expiry baseline directly.
    far_future = datetime.now(timezone.utc) + timedelta(hours=GMAIL_RENEW_HOURS + 24)
    state.put("me", history_id="50", expiration=far_future)

    gmail = _FakeGmailService()  # would raise if called; watch() unused here
    result = ensure_watch(gmail, state, email="me", topic="projects/p/topics/t")

    assert result.renewed is False
    assert result.history_id == "50"


def test_gmail_watch_state_triggers_renewal_when_near_expiry(tmp_path):
    path = tmp_path / "watch.json"
    state = JsonGmailWatchState(str(path))
    near_expiry = datetime.now(timezone.utc) + timedelta(hours=GMAIL_RENEW_HOURS - 1)
    state.put("me", history_id="50", expiration=near_expiry)

    new_exp_ms = int((datetime.now(timezone.utc) + timedelta(days=7)).timestamp() * 1000)
    gmail = _FakeGmailService(history_id="999", expiration_ms=new_exp_ms)
    result = ensure_watch(gmail, state, email="me", topic="projects/p/topics/t")

    assert result.renewed is True
    assert result.history_id == "999"
    # And the renewal is itself durable through this same state class.
    assert state.get("me")["history_id"] == "999"


# ---------------------------------------------------------------------------
# JsonChatSubscriptionState — basic persistence
# ---------------------------------------------------------------------------


def test_chat_subscription_state_roundtrips_name(tmp_path):
    path = tmp_path / "sub.json"
    state = JsonChatSubscriptionState(str(path))
    exp = datetime.now(timezone.utc) + timedelta(days=7)

    state.put("spaces/ABC", subscription_name="subscriptions/1", expiration=exp)
    stored = state.get("spaces/ABC")

    assert stored["subscription_name"] == "subscriptions/1"


def test_chat_subscription_state_missing_key_returns_none(tmp_path):
    state = JsonChatSubscriptionState(str(tmp_path / "sub.json"))
    assert state.get("spaces/NONE") is None


def test_chat_subscription_state_persists_across_instances(tmp_path):
    path = tmp_path / "sub.json"
    exp = datetime.now(timezone.utc) + timedelta(days=7)
    JsonChatSubscriptionState(str(path)).put(
        "spaces/X", subscription_name="subscriptions/x", expiration=exp
    )
    reloaded = JsonChatSubscriptionState(str(path))
    assert reloaded.get("spaces/X")["subscription_name"] == "subscriptions/x"


# ---------------------------------------------------------------------------
# JsonChatSubscriptionState — round-trips correctly through
# ensure_subscription's own expiration-parsing path
# ---------------------------------------------------------------------------


class _FakeWorkspaceEventsService:
    def __init__(self, name="subscriptions/new", expire_time=None):
        self._name = name
        self._expire_time = expire_time

    def subscriptions(self):
        svc = self

        class _Subs:
            def create(self, body):
                class _Req:
                    def execute(self_):
                        return {"name": svc._name, "expireTime": svc._expire_time}
                return _Req()
        return _Subs()


def test_chat_subscription_state_skips_renewal_when_fresh(tmp_path):
    path = tmp_path / "sub.json"
    state = JsonChatSubscriptionState(str(path))
    far_future = datetime.now(timezone.utc) + timedelta(hours=CHAT_RENEW_HOURS + 24)
    state.put("spaces/ABC", subscription_name="subscriptions/old", expiration=far_future)

    svc = _FakeWorkspaceEventsService()
    result = ensure_subscription(svc, state, space="spaces/ABC", topic="projects/p/topics/t")

    assert result.renewed is False
    assert result.subscription_name == "subscriptions/old"


def test_chat_subscription_state_triggers_renewal_when_near_expiry(tmp_path):
    path = tmp_path / "sub.json"
    state = JsonChatSubscriptionState(str(path))
    near_expiry = datetime.now(timezone.utc) + timedelta(hours=CHAT_RENEW_HOURS - 1)
    state.put("spaces/ABC", subscription_name="subscriptions/old", expiration=near_expiry)

    new_expire = (datetime.now(timezone.utc) + timedelta(days=7)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    svc = _FakeWorkspaceEventsService(name="subscriptions/new", expire_time=new_expire)
    result = ensure_subscription(svc, state, space="spaces/ABC", topic="projects/p/topics/t")

    assert result.renewed is True
    assert result.subscription_name == "subscriptions/new"
    assert state.get("spaces/ABC")["subscription_name"] == "subscriptions/new"
