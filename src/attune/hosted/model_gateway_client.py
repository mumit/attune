"""Response-bounded authenticated client for the hosted model gateway."""

from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import urlsplit

from .audit_client import _google_id_token
from .model_gateway import (
    MAX_EMBED_DIMENSIONS,
    MAX_GATEWAY_RESPONSE_BYTES,
    MAX_RESPONSE_CHARS,
    MAX_TOKEN_COUNT,
    PROFILE_NAME,
    TokenUsage,
    validate_embed_input,
    validate_messages,
)

TokenProvider = Callable[[str], str]
UsageSink = Callable[["TokenUsage | None"], None]


def _parse_gateway_usage(value: object) -> TokenUsage | None:
    """Strict parsing of THIS gateway's own versioned wire contract -- unlike
    ``model_gateway._provider_usage``'s defensive tolerance of an untrusted
    third-party provider shape, a malformed ``usage`` field here means the
    gateway and client have drifted out of sync, which is a hard contract
    violation exactly like a malformed ``text``/``vector`` field already is."""
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"input_tokens", "output_tokens"}
    ):
        raise RuntimeError("model gateway response is invalid")
    input_tokens, output_tokens = value["input_tokens"], value["output_tokens"]
    for count in (input_tokens, output_tokens):
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or not 0 <= count <= MAX_TOKEN_COUNT
        ):
            raise RuntimeError("model gateway response is invalid")
    return TokenUsage(input_tokens, output_tokens)


def _validated_profile(profile: str | None) -> str | None:
    if profile is not None and (
        not isinstance(profile, str) or not PROFILE_NAME.fullmatch(profile)
    ):
        raise ValueError("model profile is invalid")
    return profile


class ModelGatewayClient:
    def __init__(
        self,
        service_url: str,
        audience: str,
        *,
        token_provider: TokenProvider | None = None,
        session: Any | None = None,
        timeout_seconds: float = 25.0,
    ):
        self._service_url = _https_origin(service_url)
        self._audience = _https_origin(audience)
        if not 1 <= timeout_seconds <= 30:
            raise ValueError("model gateway timeout must be between 1 and 30 seconds")
        self._token_provider = token_provider or _google_id_token
        self._session = session
        self._timeout = timeout_seconds

    def complete(
        self,
        *,
        task: str,
        messages: object,
        profile: str | None = None,
        usage_sink: UsageSink | None = None,
    ) -> str:
        profile = _validated_profile(profile)
        normalized = validate_messages(task=task, messages=messages)
        import requests

        token = self._token_provider(self._audience)
        if not token or any(character.isspace() for character in token):
            raise RuntimeError("model gateway identity token is unavailable")
        session = self._session or requests.Session()
        if self._session is None:
            session.trust_env = False
        request_body: dict[str, Any] = {
            "version": 1, "task": task, "messages": normalized,
        }
        if profile is not None:
            request_body["profile"] = profile
        response = session.post(
            f"{self._service_url}/v1/models/complete",
            json=request_body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._timeout,
            allow_redirects=False,
            stream=True,
        )
        try:
            if response.status_code != 200:
                raise RuntimeError("model gateway request failed")
            raw = response.raw.read(
                MAX_GATEWAY_RESPONSE_BYTES + 1, decode_content=True
            )
            if len(raw) > MAX_GATEWAY_RESPONSE_BYTES:
                raise RuntimeError("model gateway response is too large")
            body = json.loads(raw)
            if not isinstance(body, dict) or set(body) != {"text", "usage"}:
                raise RuntimeError("model gateway response is invalid")
            text = body["text"]
            if not isinstance(text, str) or not 1 <= len(text) <= MAX_RESPONSE_CHARS:
                raise RuntimeError("model gateway response is invalid")
            usage = _parse_gateway_usage(body["usage"])
            if usage_sink is not None:
                usage_sink(usage)
            return text
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("model gateway response is invalid") from error
        finally:
            response.close()

    def embed(
        self,
        *,
        text: str,
        profile: str | None = None,
        usage_sink: UsageSink | None = None,
    ) -> tuple[float, ...]:
        profile = _validated_profile(profile)
        normalized = validate_embed_input(text)
        import requests

        token = self._token_provider(self._audience)
        if not token or any(character.isspace() for character in token):
            raise RuntimeError("model gateway identity token is unavailable")
        session = self._session or requests.Session()
        if self._session is None:
            session.trust_env = False
        request_body: dict[str, Any] = {
            "version": 1, "task": "embed", "input": normalized,
        }
        if profile is not None:
            request_body["profile"] = profile
        response = session.post(
            f"{self._service_url}/v1/models/embed",
            json=request_body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._timeout,
            allow_redirects=False,
            stream=True,
        )
        try:
            if response.status_code != 200:
                raise RuntimeError("model gateway request failed")
            raw = response.raw.read(
                MAX_GATEWAY_RESPONSE_BYTES + 1, decode_content=True
            )
            if len(raw) > MAX_GATEWAY_RESPONSE_BYTES:
                raise RuntimeError("model gateway response is too large")
            body = json.loads(raw)
            if not isinstance(body, dict) or set(body) != {"vector", "usage"}:
                raise RuntimeError("model gateway response is invalid")
            vector = body["vector"]
            if not isinstance(vector, list) or not 1 <= len(vector) <= MAX_EMBED_DIMENSIONS:
                raise RuntimeError("model gateway response is invalid")
            values: list[float] = []
            for value in vector:
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise RuntimeError("model gateway response is invalid")
                values.append(float(value))
            usage = _parse_gateway_usage(body["usage"])
            if usage_sink is not None:
                usage_sink(usage)
            return tuple(values)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("model gateway response is invalid") from error
        finally:
            response.close()


def _https_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("model gateway endpoint must be an HTTPS origin")
    return value.rstrip("/")
