"""Tests for DirectOAuthConnector.

All tests run offline: real Google credentials and network calls are replaced
by injected fake service objects. The send gate, provenance tagging, MIME
construction, label resolution, and Calendar wiring are all exercised here.
"""

from __future__ import annotations

import base64
import email
from datetime import datetime, timezone

import pytest

from aidedecamp.connectors.direct_oauth import (
    DirectOAuthConnector,
    _build_raw,
    _decode_body,
    _event_from_google,
    _header,
    _thread_from_full,
    _thread_from_metadata,
    _to_rfc3339,
)
from aidedecamp.connectors.base import Provenance, SendNotPermitted


# ---------------------------------------------------------------------------
# Fake Google service objects
# ---------------------------------------------------------------------------

class _Exec:
    """Wraps a result dict so .execute() returns it."""
    def __init__(self, result: dict):
        self._r = result
    def execute(self):
        return self._r


class FakeGmail:
    """Minimal fake gmail service. Set _*_result attributes before each test."""

    def __init__(self):
        self.calls: list[tuple] = []
        self._thread_list: dict = {"threads": []}
        self._thread_get: dict = {}
        self._thread_modify: dict = {}
        self._draft_create: dict = {"id": "d1", "message": {"id": "m1", "threadId": "t1"}}
        self._draft_send: dict = {"id": "m2", "threadId": "t1"}
        self._labels_list: dict = {"labels": []}
        self._labels_create: dict = {"id": "Label_new", "name": "Followup"}

    def users(self):
        return self

    def threads(self):
        return _FakeThreads(self)

    def drafts(self):
        return _FakeDrafts(self)

    def labels(self):
        return _FakeLabels(self)


class _FakeThreads:
    def __init__(self, g: FakeGmail):
        self._g = g

    def list(self, **kw):
        self._g.calls.append(("threads.list", kw))
        return _Exec(self._g._thread_list)

    def get(self, **kw):
        self._g.calls.append(("threads.get", kw))
        return _Exec(self._g._thread_get)

    def modify(self, **kw):
        self._g.calls.append(("threads.modify", kw))
        return _Exec(self._g._thread_modify)


class _FakeDrafts:
    def __init__(self, g: FakeGmail):
        self._g = g

    def create(self, **kw):
        self._g.calls.append(("drafts.create", kw))
        return _Exec(self._g._draft_create)

    def send(self, **kw):
        self._g.calls.append(("drafts.send", kw))
        return _Exec(self._g._draft_send)


class _FakeLabels:
    def __init__(self, g: FakeGmail):
        self._g = g

    def list(self, **kw):
        self._g.calls.append(("labels.list", kw))
        return _Exec(self._g._labels_list)

    def create(self, **kw):
        self._g.calls.append(("labels.create", kw))
        return _Exec(self._g._labels_create)


class FakeCalendar:
    def __init__(self):
        self.calls: list[tuple] = []
        self._events_list: dict = {"items": []}
        self._events_insert: dict = {"id": "ev1"}
        self._events_get: dict = {}

    def events(self):
        return _FakeEvents(self)


class _FakeEvents:
    def __init__(self, c: FakeCalendar):
        self._c = c

    def list(self, **kw):
        self._c.calls.append(("events.list", kw))
        return _Exec(self._c._events_list)

    def get(self, **kw):
        self._c.calls.append(("events.get", kw))
        return _Exec(self._c._events_get)

    def insert(self, **kw):
        self._c.calls.append(("events.insert", kw))
        return _Exec(self._c._events_insert)


# Canonical single-message thread returned by threads.get(format='metadata')
_METADATA_THREAD = {
    "id": "t1",
    "messages": [
        {
            "id": "m1",
            "snippet": "Can we move Thursday?",
            "labelIds": ["INBOX", "UNREAD"],
            "internalDate": "1720000000000",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Reschedule?"},
                    {"name": "From", "value": "vendor@acme.com"},
                ]
            },
        }
    ],
}

# Thread returned by threads.get(format='full') — body in base64url
_PLAIN_BODY = "Hello, can we reschedule Thursday's call?"
_FULL_THREAD = {
    "id": "t1",
    "messages": [
        {
            "id": "m1",
            "snippet": "Can we move Thursday?",
            "labelIds": ["INBOX"],
            "internalDate": "1720000000000",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Reschedule?"},
                    {"name": "From", "value": "vendor@acme.com"},
                ],
                "mimeType": "text/plain",
                "body": {
                    "data": base64.urlsafe_b64encode(
                        _PLAIN_BODY.encode()
                    ).decode(),
                },
            },
        }
    ],
}


def _conn(gmail=None, calendar=None, send_enabled=False):
    return DirectOAuthConnector(
        gmail_service=gmail or FakeGmail(),
        calendar_service=calendar or FakeCalendar(),
        send_enabled=send_enabled,
    )


# ---------------------------------------------------------------------------
# list_threads
# ---------------------------------------------------------------------------


def test_list_threads_returns_provenance_fetched():
    gmail = FakeGmail()
    gmail._thread_list = {"threads": [{"id": "t1"}]}
    gmail._thread_get = _METADATA_THREAD
    conn = _conn(gmail=gmail)
    threads = conn.list_threads("is:unread")
    assert threads[0].provenance == Provenance.FETCHED


def test_list_threads_parses_subject_and_from():
    gmail = FakeGmail()
    gmail._thread_list = {"threads": [{"id": "t1"}]}
    gmail._thread_get = _METADATA_THREAD
    conn = _conn(gmail=gmail)
    t = conn.list_threads()[0]
    assert t.subject == "Reschedule?"
    assert t.from_addr == "vendor@acme.com"
    assert t.snippet == "Can we move Thursday?"
    assert t.thread_id == "t1"


def test_list_threads_empty_result():
    gmail = FakeGmail()
    gmail._thread_list = {"threads": []}
    conn = _conn(gmail=gmail)
    assert conn.list_threads() == []


def test_list_threads_passes_query_and_max():
    gmail = FakeGmail()
    gmail._thread_list = {"threads": []}
    conn = _conn(gmail=gmail)
    conn.list_threads("label:work", max_results=5)
    call_name, call_kw = gmail.calls[0]
    assert call_name == "threads.list"
    assert call_kw["q"] == "label:work"
    assert call_kw["maxResults"] == 5


# ---------------------------------------------------------------------------
# get_thread
# ---------------------------------------------------------------------------


def test_get_thread_returns_full_body():
    gmail = FakeGmail()
    gmail._thread_get = _FULL_THREAD
    conn = _conn(gmail=gmail)
    t = conn.get_thread("t1")
    assert t.body == _PLAIN_BODY
    assert t.provenance == Provenance.FETCHED


def test_get_thread_calls_full_format():
    gmail = FakeGmail()
    gmail._thread_get = _FULL_THREAD
    conn = _conn(gmail=gmail)
    conn.get_thread("t1")
    _, kw = gmail.calls[0]
    assert kw["format"] == "full"
    assert kw["id"] == "t1"


# ---------------------------------------------------------------------------
# create_draft
# ---------------------------------------------------------------------------


def test_create_draft_returns_draft_ref():
    gmail = FakeGmail()
    conn = _conn(gmail=gmail)
    ref = conn.create_draft(to="a@b.com", subject="Hi", body="Hello")
    assert ref.draft_id == "d1"
    assert ref.thread_id == "t1"


def test_create_draft_sends_base64url_raw():
    gmail = FakeGmail()
    conn = _conn(gmail=gmail)
    conn.create_draft(to="a@b.com", subject="Hi", body="Hello")
    _, kw = gmail.calls[0]
    assert kw["userId"] == "me"
    raw = kw["body"]["message"]["raw"]
    # Unwrap the outer base64url envelope to get the RFC 2822 message bytes.
    mime_bytes = base64.urlsafe_b64decode(raw + "==")
    msg = email.message_from_bytes(mime_bytes)
    assert msg["to"] == "a@b.com"
    # get_payload(decode=True) decodes the content-transfer-encoding layer.
    body = msg.get_payload(decode=True).decode("utf-8")
    assert body == "Hello"


def test_create_draft_includes_thread_id():
    gmail = FakeGmail()
    conn = _conn(gmail=gmail)
    conn.create_draft(to="a@b.com", subject="Re", body="ok", thread_id="t99")
    _, kw = gmail.calls[0]
    assert kw["body"]["message"]["threadId"] == "t99"


# ---------------------------------------------------------------------------
# send_reply — gate stays closed by default
# ---------------------------------------------------------------------------


def test_send_reply_refused_when_disabled():
    conn = _conn(send_enabled=False)
    with pytest.raises(SendNotPermitted):
        conn.send_reply(draft_id="d1")


def test_send_reply_calls_api_when_enabled():
    gmail = FakeGmail()
    conn = _conn(gmail=gmail, send_enabled=True)
    conn.send_reply(draft_id="d1")
    call_name, kw = gmail.calls[0]
    assert call_name == "drafts.send"
    assert kw["body"]["id"] == "d1"


# ---------------------------------------------------------------------------
# add_label
# ---------------------------------------------------------------------------


def test_add_label_uses_existing_label_id():
    gmail = FakeGmail()
    gmail._labels_list = {"labels": [{"id": "Label_123", "name": "Followup"}]}
    conn = _conn(gmail=gmail)
    conn.add_label(thread_id="t1", label="Followup")
    modify_call = next(c for c in gmail.calls if c[0] == "threads.modify")
    assert modify_call[1]["body"]["addLabelIds"] == ["Label_123"]


def test_add_label_creates_label_when_absent():
    gmail = FakeGmail()
    gmail._labels_list = {"labels": []}
    gmail._labels_create = {"id": "Label_new", "name": "Followup"}
    conn = _conn(gmail=gmail)
    conn.add_label(thread_id="t1", label="Followup")
    assert any(c[0] == "labels.create" for c in gmail.calls)
    modify_call = next(c for c in gmail.calls if c[0] == "threads.modify")
    assert modify_call[1]["body"]["addLabelIds"] == ["Label_new"]


def test_add_label_match_is_case_insensitive():
    gmail = FakeGmail()
    gmail._labels_list = {"labels": [{"id": "Label_X", "name": "followup"}]}
    conn = _conn(gmail=gmail)
    conn.add_label(thread_id="t1", label="FOLLOWUP")
    modify_call = next(c for c in gmail.calls if c[0] == "threads.modify")
    assert modify_call[1]["body"]["addLabelIds"] == ["Label_X"]
    assert not any(c[0] == "labels.create" for c in gmail.calls)


# ---------------------------------------------------------------------------
# list_events
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
_LATER = datetime(2026, 7, 10, 17, 0, tzinfo=timezone.utc)

_GOOGLE_EVENT = {
    "id": "ev1",
    "summary": "Team sync",
    "start": {"dateTime": "2026-07-10T09:00:00+00:00"},
    "end": {"dateTime": "2026-07-10T10:00:00+00:00"},
    "attendees": [{"email": "alice@corp.com"}, {"email": "bob@telus.com"}],
}


def test_list_events_returns_calendar_events():
    cal = FakeCalendar()
    cal._events_list = {"items": [_GOOGLE_EVENT]}
    conn = _conn(calendar=cal)
    events = conn.list_events(time_min=_NOW, time_max=_LATER)
    assert len(events) == 1
    e = events[0]
    assert e.event_id == "ev1"
    assert e.summary == "Team sync"
    assert e.start == datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)


def test_list_events_flags_external_attendees():
    cal = FakeCalendar()
    cal._events_list = {"items": [_GOOGLE_EVENT]}
    conn = _conn(calendar=cal)
    events = conn.list_events(time_min=_NOW, time_max=_LATER)
    # alice@corp.com is external (not @telus.com)
    assert events[0].external_attendees is True


def test_list_events_passes_time_window():
    cal = FakeCalendar()
    cal._events_list = {"items": []}
    conn = _conn(calendar=cal)
    conn.list_events(time_min=_NOW, time_max=_LATER)
    _, kw = cal.calls[0]
    assert kw["timeMin"] == _NOW.isoformat()
    assert kw["timeMax"] == _LATER.isoformat()
    assert kw["singleEvents"] is True


def test_list_events_empty_result():
    cal = FakeCalendar()
    cal._events_list = {"items": []}
    conn = _conn(calendar=cal)
    assert conn.list_events(time_min=_NOW, time_max=_LATER) == []


# ---------------------------------------------------------------------------
# get_event
# ---------------------------------------------------------------------------


def test_get_event_returns_calendar_event():
    cal = FakeCalendar()
    cal._events_get = {
        "id": "e1",
        "summary": "1:1",
        "start": {"dateTime": _NOW.isoformat()},
        "end": {"dateTime": _LATER.isoformat()},
    }
    conn = _conn(calendar=cal)
    event = conn.get_event("e1")
    assert event.event_id == "e1"
    assert event.summary == "1:1"
    assert event.start == _NOW


def test_get_event_passes_event_id_and_calendar_id():
    cal = FakeCalendar()
    cal._events_get = {"id": "e1", "start": {"dateTime": _NOW.isoformat()}, "end": {"dateTime": _LATER.isoformat()}}
    conn = _conn(calendar=cal)
    conn.get_event("e1")
    _, kw = cal.calls[0]
    assert kw["eventId"] == "e1"
    assert kw["calendarId"] == "primary"


# ---------------------------------------------------------------------------
# create_hold
# ---------------------------------------------------------------------------


def test_create_hold_inserts_tentative_event():
    from aidedecamp.connectors.base import CalendarEvent
    cal = FakeCalendar()
    cal._events_insert = {"id": "hold99"}
    conn = _conn(calendar=cal)
    ev = CalendarEvent(
        event_id="",
        summary="Hold: call with vendor",
        start=_NOW,
        end=_LATER,
        attendees=["vendor@acme.com"],
    )
    hold_id = conn.create_hold(ev)
    assert hold_id == "hold99"
    _, kw = cal.calls[0]
    assert kw["body"]["status"] == "tentative"
    assert kw["body"]["summary"] == "Hold: call with vendor"


# ---------------------------------------------------------------------------
# Pure helper unit tests (no service needed)
# ---------------------------------------------------------------------------


def test_build_raw_produces_valid_mime():
    raw = _build_raw(to="a@b.com", subject="Test", body="Hello")
    decoded = base64.urlsafe_b64decode(raw + "==").decode("utf-8", errors="replace")
    msg = email.message_from_string(decoded)
    assert msg["to"] == "a@b.com"
    assert msg["subject"] == "Test"


def test_decode_body_simple():
    data = base64.urlsafe_b64encode(b"simple body").decode()
    payload = {"body": {"data": data}}
    assert _decode_body(payload) == "simple body"


def test_decode_body_multipart():
    data = base64.urlsafe_b64encode(b"plain part").decode()
    payload = {
        "mimeType": "multipart/alternative",
        "body": {},
        "parts": [
            {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(b"<b>html</b>").decode()}},
            {"mimeType": "text/plain", "body": {"data": data}},
        ],
    }
    assert _decode_body(payload) == "plain part"


def test_header_case_insensitive():
    msg = {"payload": {"headers": [{"name": "Subject", "value": "Hello"}]}}
    assert _header(msg, "subject") == "Hello"
    assert _header(msg, "SUBJECT") == "Hello"


def test_to_rfc3339_adds_utc_when_naive():
    naive = datetime(2026, 7, 10, 9, 0)
    result = _to_rfc3339(naive)
    assert "+00:00" in result or "Z" in result or result.endswith("+00:00")


def test_event_from_google_all_day():
    data = {
        "id": "ev2",
        "summary": "Holiday",
        "start": {"date": "2026-07-11"},
        "end": {"date": "2026-07-12"},
        "attendees": [],
    }
    ev = _event_from_google(data)
    assert ev.start.tzinfo is not None
    assert ev.summary == "Holiday"


# --- reply envelope (prompt 18) --------------------------------------------

def _msg(from_addr, *, reply_to=None, ts="1720000000000"):
    headers = [{"name": "From", "value": from_addr}]
    if reply_to:
        headers.append({"name": "Reply-To", "value": reply_to})
    headers.append({"name": "Subject", "value": "Thread subject"})
    return {"snippet": "s", "internalDate": ts, "labelIds": [],
            "payload": {"headers": headers}}


def test_reply_to_targets_newest_counterparty():
    """The owner started the thread, the counterparty replied twice: reply_to
    is the NEWEST counterparty message's From — never the owner."""
    data = {"id": "t1", "messages": [
        _msg("Me <me@example.com>"),
        _msg("Ann <ann@x.com>"),
        _msg("Me <me@example.com>"),
        _msg("Bob <bob@x.com>"),
    ]}
    thread = _thread_from_metadata(data, owner_email="me@example.com")
    assert thread.reply_to == "Bob <bob@x.com>"


def test_reply_to_prefers_reply_to_header():
    data = {"id": "t1", "messages": [
        _msg("Me <me@example.com>"),
        _msg("noreply@corp.com", reply_to="support@corp.com"),
    ]}
    thread = _thread_from_metadata(data, owner_email="me@example.com")
    assert thread.reply_to == "support@corp.com"


def test_reply_to_empty_for_owner_only_thread():
    data = {"id": "t1", "messages": [
        _msg("Me <me@example.com>"),
        _msg("me@example.com"),
    ]}
    thread = _thread_from_metadata(data, owner_email="me@example.com")
    assert thread.reply_to == ""


def test_reply_to_without_owner_falls_back_to_newest():
    data = {"id": "t1", "messages": [
        _msg("ann@x.com"),
        _msg("bob@x.com"),
    ]}
    thread = _thread_from_metadata(data)  # no owner known
    assert thread.reply_to == "bob@x.com"
