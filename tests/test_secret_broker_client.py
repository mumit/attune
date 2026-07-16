from __future__ import annotations

import json
from uuid import UUID

import pytest

from attune.hosted.secret_broker_client import (
    MAX_BROKER_RESPONSE_BYTES,
    SecretBrokerClient,
)

INTENT = UUID("10000000-0000-4000-8000-000000000521")
URL = "https://attune-secret-broker.example.run.app"
AUDIENCE = "https://attune-secret-broker.attune.internal"


class Response:
    def __init__(self, *, status=200, body=None, headers=None, chunks=None):
        self.status_code = status
        self.body = body or {
            "history_id": "123",
            "messages_total": 4,
            "threads_total": 3,
        }
        self.headers = headers or {"Content-Type": "application/json"}
        self.chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size):
        assert chunk_size == 4096
        if self.chunks is not None:
            yield from self.chunks
        else:
            yield json.dumps(self.body).encode()

    def close(self):
        self.closed = True


class Session:
    def __init__(self, response=None):
        self.response = response or Response()
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_client_uses_exact_route_audience_and_minimized_contract():
    session = Session()
    audiences = []
    profile = SecretBrokerClient(
        URL,
        AUDIENCE,
        token_provider=lambda audience: audiences.append(audience) or "token",
        session=session,
    ).google_gmail_profile(INTENT)
    assert audiences == [AUDIENCE]
    assert (profile.history_id, profile.messages_total, profile.threads_total) == (
        "123",
        4,
        3,
    )
    assert session.calls == [
        (
            f"{URL}/v1/providers/google/gmail/profile",
            {
                "json": {"intent_id": str(INTENT)},
                "headers": {"Authorization": "Bearer token"},
                "timeout": 15.0,
                "allow_redirects": False,
                "stream": True,
            },
        )
    ]
    assert session.response.closed


def test_calendar_client_requires_exact_204_empty_contract():
    response = Response(status=204, chunks=[])
    session = Session(response)
    result = SecretBrokerClient(
        URL,
        AUDIENCE,
        token_provider=lambda audience: "token",
        session=session,
    ).google_calendar_primary(INTENT)
    assert result is None
    assert session.calls[0][0] == f"{URL}/v1/providers/google/calendar/primary"
    assert response.closed

    for ambiguous in (Response(status=200, chunks=[]), Response(status=204, chunks=[b"x"])):
        with pytest.raises(RuntimeError):
            SecretBrokerClient(
                URL,
                AUDIENCE,
                token_provider=lambda audience: "token",
                session=Session(ambiguous),
            ).google_calendar_primary(INTENT)
        assert ambiguous.closed


@pytest.mark.parametrize(
    "url",
    [
        "http://broker.example.run.app",
        "https://user@broker.example.run.app",
        "https://broker.example.run.app/path",
        "https://broker.example.run.app?target=other",
    ],
)
def test_client_rejects_non_origin_url_or_audience(url):
    with pytest.raises(ValueError):
        SecretBrokerClient(url, AUDIENCE)
    with pytest.raises(ValueError):
        SecretBrokerClient(URL, url)


@pytest.mark.parametrize(
    "response",
    [
        Response(status=503),
        Response(headers={"Content-Type": "text/html"}),
        Response(body={"history_id": "123", "messages_total": 4}),
        Response(
            body={
                "history_id": "123",
                "messages_total": True,
                "threads_total": 3,
            }
        ),
        Response(chunks=[b"x" * (MAX_BROKER_RESPONSE_BYTES + 1)]),
        Response(
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(MAX_BROKER_RESPONSE_BYTES + 1),
            }
        ),
        Response(
            headers={
                "Content-Type": "application/json",
                "Content-Length": "-1",
            }
        ),
    ],
)
def test_client_fails_closed_on_broker_response_ambiguity(response):
    with pytest.raises(RuntimeError):
        SecretBrokerClient(
            URL,
            AUDIENCE,
            token_provider=lambda audience: "token",
            session=Session(response),
        ).google_gmail_profile(INTENT)
    assert response.closed


def test_default_session_ignores_ambient_proxy_and_netrc(monkeypatch):
    session = Session()
    session.trust_env = True
    monkeypatch.setattr("requests.Session", lambda: session)
    SecretBrokerClient(
        URL,
        AUDIENCE,
        token_provider=lambda audience: "token",
    ).google_gmail_profile(INTENT)
    assert session.trust_env is False
