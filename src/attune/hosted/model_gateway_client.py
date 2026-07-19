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
    validate_embed_input,
    validate_messages,
)

TokenProvider = Callable[[str], str]


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

    def complete(self, *, task: str, messages: object) -> str:
        normalized = validate_messages(task=task, messages=messages)
        import requests

        token = self._token_provider(self._audience)
        if not token or any(character.isspace() for character in token):
            raise RuntimeError("model gateway identity token is unavailable")
        session = self._session or requests.Session()
        if self._session is None:
            session.trust_env = False
        response = session.post(
            f"{self._service_url}/v1/models/complete",
            json={"version": 1, "task": task, "messages": normalized},
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
            if not isinstance(body, dict) or set(body) != {"text"}:
                raise RuntimeError("model gateway response is invalid")
            text = body["text"]
            if not isinstance(text, str) or not 1 <= len(text) <= MAX_RESPONSE_CHARS:
                raise RuntimeError("model gateway response is invalid")
            return text
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("model gateway response is invalid") from error
        finally:
            response.close()

    def embed(self, *, text: str) -> tuple[float, ...]:
        normalized = validate_embed_input(text)
        import requests

        token = self._token_provider(self._audience)
        if not token or any(character.isspace() for character in token):
            raise RuntimeError("model gateway identity token is unavailable")
        session = self._session or requests.Session()
        if self._session is None:
            session.trust_env = False
        response = session.post(
            f"{self._service_url}/v1/models/embed",
            json={"version": 1, "task": "embed", "input": normalized},
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
            if not isinstance(body, dict) or set(body) != {"vector"}:
                raise RuntimeError("model gateway response is invalid")
            vector = body["vector"]
            if not isinstance(vector, list) or not 1 <= len(vector) <= MAX_EMBED_DIMENSIONS:
                raise RuntimeError("model gateway response is invalid")
            values: list[float] = []
            for value in vector:
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise RuntimeError("model gateway response is invalid")
                values.append(float(value))
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
