"""Tests for bearer-openai.

We avoid real network calls: token resolution is tested directly, and the 401
translation is tested by monkeypatching the underlying ``chat.completions.create``
to raise the SDK's auth errors.
"""

from __future__ import annotations

import httpx
import openai
import pytest

from bearer_openai import (
    AsyncBearerClient,
    BearerClient,
    TokenNotConfiguredError,
    TokenRejectedError,
    resolve_token,
)

BASE = "https://api.example-gateway.test"


# ---------------------------------------------------------------------------
# resolve_token
# ---------------------------------------------------------------------------

def test_resolve_prefers_explicit_arg(monkeypatch):
    monkeypatch.setenv("BEARER_OPENAI_TOKEN", "from-env")
    assert resolve_token("explicit") == "explicit"


def test_resolve_uses_named_env_before_fallback(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "named")
    monkeypatch.setenv("BEARER_OPENAI_TOKEN", "fallback")
    assert resolve_token(env_var="MY_TOKEN") == "named"


def test_resolve_uses_fallback_env(monkeypatch):
    monkeypatch.delenv("MY_TOKEN", raising=False)
    monkeypatch.setenv("BEARER_OPENAI_TOKEN", "fallback")
    assert resolve_token(env_var="MY_TOKEN") == "fallback"


def test_resolve_raises_when_missing(monkeypatch):
    monkeypatch.delenv("BEARER_OPENAI_TOKEN", raising=False)
    monkeypatch.delenv("MY_TOKEN", raising=False)
    with pytest.raises(TokenNotConfiguredError) as ei:
        resolve_token(env_var="MY_TOKEN")
    # Error names both env vars it tried, to make the fix obvious.
    assert "MY_TOKEN" in str(ei.value)
    assert "BEARER_OPENAI_TOKEN" in str(ei.value)


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------

def test_missing_token_fails_at_construction(monkeypatch):
    monkeypatch.delenv("BEARER_OPENAI_TOKEN", raising=False)
    with pytest.raises(TokenNotConfiguredError):
        BearerClient(base_url=BASE)


def test_token_becomes_api_key(monkeypatch):
    client = BearerClient(base_url=BASE, token="secret-123")
    assert client.api_key == "secret-123"
    assert str(client.base_url).rstrip("/") == BASE


def test_real_sdk_request_builder_keeps_url_object():
    """Exercise below the mocked resource method that hid an SDK collision."""
    def respond(request):
        assert request.headers["authorization"] == "Bearer secret-123"
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 0,
                "model": "test",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }],
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(respond))
    client = BearerClient(
        base_url=BASE, token="secret-123", http_client=http_client
    )

    result = client.chat_completions_create(
        model="test", messages=[{"role": "user", "content": "ping"}]
    )

    assert result.choices[0].message.content == "ok"


# ---------------------------------------------------------------------------
# 401 translation
# ---------------------------------------------------------------------------

def _fake_auth_error():
    # Construct the SDK AuthenticationError shape without a live request.
    class _Resp:
        status_code = 401
        headers: dict = {}
        request = None

    return openai.AuthenticationError(
        message="Unauthorized", response=_Resp(), body=None
    )


def test_chat_401_becomes_token_rejected(monkeypatch):
    client = BearerClient(base_url=BASE, token="expired")

    def boom(**_kwargs):
        raise _fake_auth_error()

    monkeypatch.setattr(client.chat.completions, "create", boom)

    with pytest.raises(TokenRejectedError) as ei:
        client.chat_completions_create(
            model="whatever", messages=[{"role": "user", "content": "hi"}]
        )
    msg = str(ei.value)
    assert "MANUAL ROTATION" in msg
    assert BASE in msg
    # original error preserved for debugging
    assert isinstance(ei.value.__cause__, openai.AuthenticationError)


def test_non_401_errors_pass_through(monkeypatch):
    client = BearerClient(base_url=BASE, token="ok")

    def boom(**_kwargs):
        raise ValueError("some other failure")

    monkeypatch.setattr(client.chat.completions, "create", boom)

    with pytest.raises(ValueError, match="some other failure"):
        client.chat_completions_create(
            model="whatever", messages=[{"role": "user", "content": "hi"}]
        )


@pytest.mark.asyncio
async def test_async_401_becomes_token_rejected(monkeypatch):
    client = AsyncBearerClient(base_url=BASE, token="expired")

    async def boom(**_kwargs):
        raise _fake_auth_error()

    monkeypatch.setattr(client.chat.completions, "create", boom)

    with pytest.raises(TokenRejectedError):
        await client.chat_completions_create(
            model="whatever", messages=[{"role": "user", "content": "hi"}]
        )
