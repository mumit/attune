import io
import json

import pytest

from attune.hosted.model_gateway import MAX_EMBED_DIMENSIONS, MAX_GATEWAY_RESPONSE_BYTES
from attune.hosted.model_gateway_client import ModelGatewayClient


class Response:
    def __init__(self, body, status=200):
        self.status_code = status
        self.raw = Raw(body)
        self.closed = False

    def close(self):
        self.closed = True


class Raw(io.BytesIO):
    def read(self, size=-1, *, decode_content=False):
        return super().read(size)


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def client(response):
    session = Session(response)
    instance = ModelGatewayClient(
        "https://gateway.example",
        "https://model.attune.internal",
        token_provider=lambda audience: "worker-token",
        session=session,
    )
    return instance, session


def messages():
    return [{"role": "system", "content": "boundary"}]


def test_client_is_authenticated_bounded_and_does_not_follow_redirects():
    response = Response(json.dumps({"text": "answer"}).encode())
    instance, session = client(response)
    assert instance.complete(task="converse", messages=messages()) == "answer"
    url, kwargs = session.calls[0]
    assert url == "https://gateway.example/v1/models/complete"
    assert kwargs["headers"] == {"Authorization": "Bearer worker-token"}
    assert kwargs["allow_redirects"] is False
    assert kwargs["stream"] is True
    assert kwargs["json"] == {
        "version": 1,
        "task": "converse",
        "messages": messages(),
    }
    assert response.closed


@pytest.mark.parametrize(
    "response",
    [
        Response(b"{}", status=302),
        Response(b"not-json"),
        Response(b'{"text":"ok","model":"bad"}'),
        Response(b"x" * (MAX_GATEWAY_RESPONSE_BYTES + 1)),
    ],
)
def test_client_fails_closed_on_status_schema_and_size(response):
    instance, _ = client(response)
    with pytest.raises(RuntimeError):
        instance.complete(task="converse", messages=messages())
    assert response.closed


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://gateway.example",
        "https://user@gateway.example",
        "https://gateway.example/path",
        "https://gateway.example?next=evil",
    ],
)
def test_client_requires_fixed_https_origins(endpoint):
    with pytest.raises(ValueError):
        ModelGatewayClient(endpoint, "https://model.attune.internal")


def test_client_embed_is_authenticated_bounded_and_does_not_follow_redirects():
    response = Response(json.dumps({"vector": [0.1, 0.2, 0.3]}).encode())
    instance, session = client(response)
    assert instance.embed(text="hello") == (0.1, 0.2, 0.3)
    url, kwargs = session.calls[0]
    assert url == "https://gateway.example/v1/models/embed"
    assert kwargs["headers"] == {"Authorization": "Bearer worker-token"}
    assert kwargs["allow_redirects"] is False
    assert kwargs["stream"] is True
    assert kwargs["json"] == {"version": 1, "task": "embed", "input": "hello"}
    assert response.closed


@pytest.mark.parametrize(
    "response",
    [
        Response(b"{}", status=302),
        Response(b"not-json"),
        Response(b'{"vector":[],"model":"bad"}'),
        Response(json.dumps({"vector": [0] * (MAX_EMBED_DIMENSIONS + 1)}).encode()),
        Response(b"x" * (MAX_GATEWAY_RESPONSE_BYTES + 1)),
    ],
)
def test_client_embed_fails_closed_on_status_schema_and_size(response):
    instance, _ = client(response)
    with pytest.raises(RuntimeError):
        instance.embed(text="hello")
    assert response.closed


def test_client_embed_rejects_invalid_input():
    response = Response(json.dumps({"vector": [0.1]}).encode())
    instance, _ = client(response)
    with pytest.raises(ValueError):
        instance.embed(text="")
