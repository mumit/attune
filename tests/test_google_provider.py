from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from attune.hosted.google_provider import (
    CALENDAR_PRIMARY_URL,
    CALENDAR_EVENTS_URL,
    CALENDAR_READONLY_SCOPE,
    GMAIL_PROFILE_URL,
    GMAIL_THREADS_URL,
    GMAIL_READONLY_SCOPE,
    GOOGLE_TOKEN_URL,
    MAX_PROVIDER_RESPONSE_BYTES,
    GoogleProvider,
    ProviderFailure,
)


class Raw:
    def __init__(self, value: bytes):
        self.value = value

    def read(self, amount, decode_content=False):
        assert decode_content is True
        return self.value[:amount]


class Response:
    def __init__(self, status, body):
        self.status_code = status
        encoded = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.raw = Raw(encoded)
        self.closed = False

    def close(self):
        self.closed = True


class Session:
    def __init__(self, token=None, profile=None, calendar=None):
        self.token = token or Response(
            200, {"access_token": "short-token", "token_type": "Bearer"}
        )
        self.profile = profile or Response(
            200,
            {
                "emailAddress": "must-not-leave-broker@example.com",
                "historyId": "1234",
                "messagesTotal": 11,
                "threadsTotal": 7,
            },
        )
        self.calendar = calendar or Response(
            200,
            {"id": "must-not-leave-broker@example.com", "timeZone": "America/Vancouver"},
        )
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return self.token

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return self.calendar if url == CALENDAR_PRIMARY_URL else self.profile


def credential(**overrides):
    value = {
        "refresh_token": "refresh-secret",
        "client_id": "client-id",
        "client_secret": "client-secret",
        "token_uri": GOOGLE_TOKEN_URL,
        "scopes": [GMAIL_READONLY_SCOPE],
    }
    value.update(overrides)
    return value


def test_profile_uses_only_fixed_endpoints_and_omits_email_address():
    session = Session()
    result = GoogleProvider(session).gmail_profile(credential())
    assert result.response() == {
        "history_id": "1234",
        "messages_total": 11,
        "threads_total": 7,
    }
    assert [call[1] for call in session.calls] == [
        GOOGLE_TOKEN_URL,
        GMAIL_PROFILE_URL,
    ]
    assert all(call[2]["allow_redirects"] is False for call in session.calls)
    assert session.calls[1][2]["headers"]["Authorization"] == "Bearer short-token"
    assert session.token.closed and session.profile.closed


def test_default_session_ignores_ambient_proxy_configuration():
    assert GoogleProvider()._session.trust_env is False


def test_calendar_primary_uses_fixed_endpoint_and_returns_no_provider_data():
    session = Session()
    result = GoogleProvider(session).calendar_primary(
        credential(scopes=[CALENDAR_READONLY_SCOPE])
    )
    assert result.__dict__ == {}
    assert [call[1] for call in session.calls] == [
        GOOGLE_TOKEN_URL,
        CALENDAR_PRIMARY_URL,
    ]
    assert all(call[2]["allow_redirects"] is False for call in session.calls)
    assert session.token.closed and session.calendar.closed


def test_unapproved_token_uri_and_missing_scope_fail_before_network():
    session = Session()
    with pytest.raises(ProviderFailure):
        GoogleProvider(session).gmail_profile(
            credential(token_uri="https://attacker.invalid/token")
        )
    with pytest.raises(ProviderFailure):
        GoogleProvider(session).gmail_profile(credential(scopes=["calendar.readonly"]))
    with pytest.raises(ProviderFailure):
        GoogleProvider(session).calendar_primary(credential())
    assert session.calls == []


@pytest.mark.parametrize(
    "token,profile",
    [
        (Response(302, {}), None),
        (Response(200, {"access_token": "bad token", "token_type": "Bearer"}), None),
        (None, Response(200, {"historyId": "abc", "messagesTotal": 1, "threadsTotal": 1})),
        (None, Response(200, b"x" * (MAX_PROVIDER_RESPONSE_BYTES + 1))),
    ],
)
def test_redirects_tokens_malformed_profiles_and_large_bodies_fail(token, profile):
    with pytest.raises(ProviderFailure):
        GoogleProvider(Session(token=token, profile=profile)).gmail_profile(credential())


def test_bounded_gmail_thread_summaries_use_only_canonical_routes():
    session = Session()
    listing = Response(200, {"threads": [{"id": "thread_1"}]})
    detail = Response(200, {"messages": [{
        "snippet": "A bounded preview",
        "payload": {"headers": [
            {"name": "Subject", "value": "Status"},
            {"name": "From", "value": "Sender <sender@example.com>"},
            {"name": "Date", "value": "Wed, 16 Jul 2026 09:00:00 -0700"},
        ]},
    }]})

    def get(url, **kwargs):
        session.calls.append(("get", url, kwargs))
        return listing if url == GMAIL_THREADS_URL else detail

    session.get = get
    result = GoogleProvider(session).gmail_threads(
        credential(), query="newer_than:7d", limit=10
    )
    assert [item.response() for item in result] == [{
        "thread_id": "thread_1", "subject": "Status",
        "sender": "Sender <sender@example.com>",
        "date": "Wed, 16 Jul 2026 09:00:00 -0700",
        "snippet": "A bounded preview",
    }]
    assert [call[1] for call in session.calls] == [
        GOOGLE_TOKEN_URL, GMAIL_THREADS_URL, f"{GMAIL_THREADS_URL}/thread_1",
    ]
    assert session.calls[1][2]["params"]["maxResults"] == 10
    assert session.calls[2][2]["params"]["format"] == "metadata"


def test_bounded_calendar_events_fix_primary_calendar_and_window():
    session = Session()
    events = Response(200, {"items": [{
        "id": "event_1", "summary": "Appointment",
        "start": {"dateTime": "2026-07-17T09:00:00-07:00"},
        "end": {"dateTime": "2026-07-17T10:00:00-07:00"},
        "location": "Office", "status": "confirmed",
    }]})
    session.get = lambda url, **kwargs: (
        session.calls.append(("get", url, kwargs)) or events
    )
    lower = datetime(2026, 7, 16, tzinfo=timezone.utc)
    upper = datetime(2026, 7, 18, tzinfo=timezone.utc)
    result = GoogleProvider(session).calendar_events(
        credential(scopes=[CALENDAR_READONLY_SCOPE]),
        time_min=lower, time_max=upper, limit=25,
    )
    assert result[0].summary == "Appointment"
    assert [call[1] for call in session.calls] == [
        GOOGLE_TOKEN_URL, CALENDAR_EVENTS_URL,
    ]
    assert session.calls[1][2]["params"]["singleEvents"] == "true"
