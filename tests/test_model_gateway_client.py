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
    response = Response(json.dumps({"text": "answer", "usage": None}).encode())
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


def test_client_omits_profile_when_none_the_gate_off_byte_identical_path():
    """No ``profile`` key at all reaches the wire when the caller passes
    none -- this is what makes the gate-off path byte-identical to the
    pre-profile request shape."""
    response = Response(json.dumps({"text": "answer", "usage": None}).encode())
    instance, session = client(response)
    instance.complete(task="converse", messages=messages())
    _, kwargs = session.calls[0]
    assert "profile" not in kwargs["json"]


def test_client_forwards_a_bounded_profile_field():
    response = Response(json.dumps({"text": "answer", "usage": None}).encode())
    instance, session = client(response)
    instance.complete(task="converse", messages=messages(), profile="premium")
    _, kwargs = session.calls[0]
    assert kwargs["json"]["profile"] == "premium"


def test_client_rejects_a_malformed_profile_before_any_network_call():
    instance, session = client(Response(b"unused"))
    with pytest.raises(ValueError):
        instance.complete(task="converse", messages=messages(), profile="Not Valid!")
    assert session.calls == []


def test_client_reports_usage_through_the_sink_and_never_via_return_value():
    response = Response(
        json.dumps(
            {"text": "answer", "usage": {"input_tokens": 12, "output_tokens": 34}}
        ).encode()
    )
    instance, _ = client(response)
    reported = []
    text = instance.complete(
        task="converse", messages=messages(), usage_sink=reported.append,
    )
    assert text == "answer"
    assert len(reported) == 1
    assert reported[0].input_tokens == 12
    assert reported[0].output_tokens == 34


def test_client_reports_none_usage_through_the_sink_when_gateway_has_none():
    response = Response(json.dumps({"text": "answer", "usage": None}).encode())
    instance, _ = client(response)
    reported = []
    instance.complete(task="converse", messages=messages(), usage_sink=reported.append)
    assert reported == [None]


@pytest.mark.parametrize(
    "usage",
    [
        "not-an-object",
        {"input_tokens": 1},
        {"input_tokens": -1, "output_tokens": 0},
        {"input_tokens": 1, "output_tokens": 1, "extra": 1},
        {"input_tokens": True, "output_tokens": 0},
    ],
)
def test_client_fails_closed_on_a_malformed_usage_field(usage):
    response = Response(json.dumps({"text": "answer", "usage": usage}).encode())
    instance, _ = client(response)
    with pytest.raises(RuntimeError):
        instance.complete(task="converse", messages=messages())


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
    response = Response(json.dumps({"vector": [0.1, 0.2, 0.3], "usage": None}).encode())
    instance, session = client(response)
    assert instance.embed(text="hello") == (0.1, 0.2, 0.3)
    url, kwargs = session.calls[0]
    assert url == "https://gateway.example/v1/models/embed"
    assert kwargs["headers"] == {"Authorization": "Bearer worker-token"}
    assert kwargs["allow_redirects"] is False
    assert kwargs["stream"] is True
    assert kwargs["json"] == {"version": 1, "task": "embed", "input": "hello"}
    assert response.closed


def test_client_embed_forwards_profile_and_reports_usage_through_the_sink():
    response = Response(
        json.dumps(
            {"vector": [0.1], "usage": {"input_tokens": 5, "output_tokens": 0}}
        ).encode()
    )
    instance, session = client(response)
    reported = []
    instance.embed(text="hello", profile="premium", usage_sink=reported.append)
    _, kwargs = session.calls[0]
    assert kwargs["json"]["profile"] == "premium"
    assert len(reported) == 1
    assert reported[0].input_tokens == 5
    assert reported[0].output_tokens == 0


@pytest.mark.parametrize(
    "response",
    [
        Response(b"{}", status=302),
        Response(b"not-json"),
        Response(b'{"vector":[],"model":"bad"}'),
        Response(
            json.dumps(
                {"vector": [0] * (MAX_EMBED_DIMENSIONS + 1), "usage": None}
            ).encode()
        ),
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
