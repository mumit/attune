from __future__ import annotations

import json

import pytest

from attune.hosted.google_provider import (
    CALENDAR_PRIMARY_URL,
    CALENDAR_READONLY_SCOPE,
    GMAIL_PROFILE_URL,
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
