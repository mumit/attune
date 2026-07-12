"""Ingestion tests with a fake Gmail client + in-memory watch state. No GCP,
no Pub/Sub, no credentials.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from aidedecamp.ingestion import (
    HistoryExpired,
    decode_pubsub_message,
    ensure_watch,
    process_notification,
)


class MemState:
    def __init__(self):
        self.d = {}

    def get(self, email):
        return self.d.get(email)

    def put(self, email, *, history_id, expiration):
        self.d[email] = {"history_id": history_id, "expiration": expiration}


# --- fake Gmail client (fluent .users().watch()/.history().list()) ---------

class _Exec:
    def __init__(self, val):
        self._val = val

    def execute(self):
        return self._val


class FakeHistory:
    def __init__(self, pages):
        self._pages = pages
        self.calls = []

    def list(self, **kw):
        self.calls.append(kw)
        token = kw.get("pageToken")
        idx = 0 if token is None else int(token)
        return _Exec(self._pages[idx])


class FakeUsers:
    def __init__(self, watch_resp=None, history=None):
        self._watch_resp = watch_resp
        self._history = history

    def watch(self, userId, body):
        return _Exec(self._watch_resp)

    def history(self):
        return self._history


class FakeGmail:
    def __init__(self, watch_resp=None, history=None):
        self._users = FakeUsers(watch_resp, history)

    def users(self):
        return self._users


def _epoch_ms(dt):
    return str(int(dt.timestamp() * 1000))


# --- watch renewal --------------------------------------------------------

def test_first_watch_registers_and_stores():
    exp = datetime.now(timezone.utc) + timedelta(days=7)
    gmail = FakeGmail(watch_resp={"historyId": "1000", "expiration": _epoch_ms(exp)})
    state = MemState()
    res = ensure_watch(gmail, state, topic="projects/p/topics/t")
    assert res.renewed and res.history_id == "1000"
    assert state.get("me")["history_id"] == "1000"


def test_watch_not_renewed_when_fresh():
    exp = datetime.now(timezone.utc) + timedelta(days=6)  # >48h left
    state = MemState()
    state.put("me", history_id="500", expiration=exp)
    gmail = FakeGmail(watch_resp={"historyId": "999", "expiration": _epoch_ms(exp)})
    res = ensure_watch(gmail, state, topic="projects/p/topics/t")
    assert not res.renewed and res.history_id == "500"


def test_watch_renewed_when_near_expiry():
    exp = datetime.now(timezone.utc) + timedelta(hours=12)  # <48h left
    state = MemState()
    state.put("me", history_id="500", expiration=exp)
    new_exp = datetime.now(timezone.utc) + timedelta(days=7)
    gmail = FakeGmail(watch_resp={"historyId": "1200", "expiration": _epoch_ms(new_exp)})
    res = ensure_watch(gmail, state, topic="projects/p/topics/t")
    assert res.renewed and res.history_id == "1200"


# --- pubsub decode --------------------------------------------------------

def test_decode_pubsub_message():
    payload = {"emailAddress": "user@x.com", "historyId": "42"}
    data = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    assert decode_pubsub_message({"data": data}) == payload


# --- history reconciliation ----------------------------------------------

def test_process_dedupes_threads_by_id():
    # two history records referencing the same thread t1, plus t2
    pages = [
        {
            "history": [
                {"messagesAdded": [{"message": {"id": "m1", "threadId": "t1"}}]},
                {"messagesAdded": [{"message": {"id": "m2", "threadId": "t1"}}]},
                {"messagesAdded": [{"message": {"id": "m3", "threadId": "t2"}}]},
            ]
        }
    ]
    gmail = FakeGmail(history=FakeHistory(pages))
    state = MemState()
    state.put("u@x.com", history_id="100", expiration=datetime.now(timezone.utc))
    changes = process_notification(
        gmail, state, {"emailAddress": "u@x.com", "historyId": "150"}
    )
    assert changes.thread_ids == ["t1", "t2"]           # deduped, order-preserved
    assert state.get("u@x.com")["history_id"] == "150"  # baseline advanced


def test_process_uses_stored_history_as_start():
    pages = [{"history": []}]
    hist = FakeHistory(pages)
    gmail = FakeGmail(history=hist)
    state = MemState()
    state.put("u@x.com", history_id="100", expiration=datetime.now(timezone.utc))
    process_notification(gmail, state, {"emailAddress": "u@x.com", "historyId": "150"})
    # reconciliation started from the STORED id (100), not the notification's 150
    assert hist.calls[0]["startHistoryId"] == "100"


def test_stale_history_raises_history_expired():
    class Boom:
        def list(self, **kw):
            class E(Exception):
                status_code = 404
            raise E("gone")

    gmail = FakeGmail(history=Boom())
    state = MemState()
    state.put("u@x.com", history_id="1", expiration=datetime.now(timezone.utc))
    with pytest.raises(HistoryExpired):
        process_notification(gmail, state, {"emailAddress": "u@x.com", "historyId": "9"})


def test_no_baseline_requires_full_sync():
    gmail = FakeGmail(history=FakeHistory([{"history": []}]))
    state = MemState()
    with pytest.raises(HistoryExpired):
        process_notification(gmail, state, {"emailAddress": "new@x.com", "historyId": "9"})


# --- email safety (prompt 18): the owner's own activity is not signal ------

def test_sent_and_draft_messages_are_not_thread_changes():
    """SENT/DRAFT-labeled additions are the owner acting (their reply, a
    draft save) — reacting would mean triaging your own words."""
    pages = [
        {
            "history": [
                {"messagesAdded": [{"message": {
                    "id": "m1", "threadId": "t1", "labelIds": ["SENT"]}}]},
                {"messagesAdded": [{"message": {
                    "id": "m2", "threadId": "t2", "labelIds": ["DRAFT"]}}]},
                {"messagesAdded": [{"message": {
                    "id": "m3", "threadId": "t3", "labelIds": ["INBOX", "UNREAD"]}}]},
            ]
        }
    ]
    gmail = FakeGmail(history=FakeHistory(pages))
    state = MemState()
    state.put("u@x.com", history_id="100", expiration=datetime.now(timezone.utc))
    changes = process_notification(
        gmail, state, {"emailAddress": "u@x.com", "historyId": "150"}
    )
    assert changes.thread_ids == ["t3"]


def test_mixed_thread_counts_when_inbound_message_present():
    """A thread with both a SENT add and an inbound add still counts —
    something genuinely arrived."""
    pages = [
        {
            "history": [
                {"messagesAdded": [{"message": {
                    "id": "m1", "threadId": "t1", "labelIds": ["SENT"]}}]},
                {"messagesAdded": [{"message": {
                    "id": "m2", "threadId": "t1", "labelIds": ["INBOX"]}}]},
            ]
        }
    ]
    gmail = FakeGmail(history=FakeHistory(pages))
    state = MemState()
    state.put("u@x.com", history_id="100", expiration=datetime.now(timezone.utc))
    changes = process_notification(
        gmail, state, {"emailAddress": "u@x.com", "historyId": "150"}
    )
    assert changes.thread_ids == ["t1"]


def test_missing_label_ids_still_counts_as_inbound():
    """No labelIds field (older API shapes) -> treated as inbound, never
    silently dropped."""
    pages = [
        {"history": [{"messagesAdded": [{"message": {"id": "m1", "threadId": "t1"}}]}]}
    ]
    gmail = FakeGmail(history=FakeHistory(pages))
    state = MemState()
    state.put("u@x.com", history_id="100", expiration=datetime.now(timezone.utc))
    changes = process_notification(
        gmail, state, {"emailAddress": "u@x.com", "historyId": "150"}
    )
    assert changes.thread_ids == ["t1"]
