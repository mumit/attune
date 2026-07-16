from uuid import UUID

import pytest

from attune.hosted.dispatch_broker_client import DispatchBrokerClient

INTENT_ID = UUID("10000000-0000-4000-8000-000000000001")


class Response:
    def __init__(self, status_code):
        self.status_code = status_code


class Session:
    def __init__(self, status_code=204):
        self.status_code = status_code
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response(self.status_code)


def test_dispatch_client_sends_only_canonical_intent_to_private_origin():
    session = Session()
    client = DispatchBrokerClient(
        "https://broker.example",
        "https://audience.example",
        session=session,
        token_provider=lambda audience: f"token-for:{audience}",
    )
    assert client.dispatch(INTENT_ID)
    assert session.calls == [
        (
            "https://broker.example/v1/dispatch-intents/dispatch",
            {
                "json": {"intent_id": str(INTENT_ID)},
                "headers": {"Authorization": "Bearer token-for:https://audience.example"},
                "timeout": 10.0,
                "allow_redirects": False,
            },
        )
    ]


def test_dispatch_client_rejects_unsafe_configuration_and_non_success():
    with pytest.raises(ValueError):
        DispatchBrokerClient("http://broker.example", "https://audience.example")
    with pytest.raises(ValueError):
        DispatchBrokerClient("https://broker.example/path", "https://audience.example")
    client = DispatchBrokerClient(
        "https://broker.example",
        "https://audience.example",
        session=Session(503),
        token_provider=lambda _audience: "token",
    )
    assert not client.dispatch(INTENT_ID)
