"""Authenticated client for fixed, response-minimized broker operations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import UUID

from .audit_client import _google_id_token

MAX_BROKER_RESPONSE_BYTES = 32_768
TokenProvider = Callable[[str], str]


@dataclass(frozen=True)
class GmailProfile:
    history_id: str
    messages_total: int
    threads_total: int


class SecretBrokerClient:
    """Invoke only the broker's compiled-in Google operations."""

    def __init__(
        self,
        service_url: str,
        audience: str,
        *,
        token_provider: TokenProvider | None = None,
        session: Any | None = None,
        timeout_seconds: float = 15.0,
    ):
        self._service_url = _https_origin(service_url, "secret broker URL")
        self._audience = _https_origin(audience, "secret broker audience")
        if not 1 <= timeout_seconds <= 30:
            raise ValueError("secret broker timeout must be between 1 and 30 seconds")
        self._token_provider = token_provider or _google_id_token
        self._session = session
        self._timeout = timeout_seconds

    def google_gmail_profile(self, intent_id: UUID) -> GmailProfile:
        response = self._post("/v1/providers/google/gmail/profile", intent_id)
        try:
            if response.status_code != 200:
                raise RuntimeError("secret broker provider operation failed")
            content_type = response.headers.get("Content-Type", "")
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                raise RuntimeError("secret broker response type is invalid")
            raw = _bounded_body(response)
            try:
                body = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise RuntimeError("secret broker response is invalid") from error
            return _profile(body)
        finally:
            _close(response)

    def google_calendar_primary(self, intent_id: UUID) -> None:
        response = self._post("/v1/providers/google/calendar/primary", intent_id)
        try:
            if response.status_code != 204:
                raise RuntimeError("secret broker provider operation failed")
            raw = _bounded_body(response)
            if raw:
                raise RuntimeError("secret broker response contract is invalid")
        finally:
            _close(response)

    def _post(self, path: str, intent_id: UUID):
        import requests

        if not isinstance(intent_id, UUID):
            raise TypeError("intent_id must be a UUID")
        token = self._token_provider(self._audience)
        if not isinstance(token, str) or not token:
            raise RuntimeError("secret broker identity token is unavailable")
        session = self._session
        if session is None:
            session = requests.Session()
            session.trust_env = False
        return session.post(
            f"{self._service_url}{path}",
            json={"intent_id": str(intent_id)},
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._timeout,
            allow_redirects=False,
            stream=True,
        )


def _https_origin(value: str, name: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(f"{name} must be an HTTPS origin")
    return value.rstrip("/")


def _bounded_body(response: Any) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            length = int(declared)
            if not 0 <= length <= MAX_BROKER_RESPONSE_BYTES:
                raise RuntimeError("secret broker response is too large")
        except ValueError as error:
            raise RuntimeError("secret broker response length is invalid") from error
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_content(chunk_size=4096):
        if not isinstance(chunk, bytes):
            raise RuntimeError("secret broker response is invalid")
        size += len(chunk)
        if size > MAX_BROKER_RESPONSE_BYTES:
            raise RuntimeError("secret broker response is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _profile(body: Any) -> GmailProfile:
    if not isinstance(body, dict) or set(body) != {
        "history_id",
        "messages_total",
        "threads_total",
    }:
        raise RuntimeError("secret broker response contract is invalid")
    history_id = body["history_id"]
    messages_total = body["messages_total"]
    threads_total = body["threads_total"]
    if not isinstance(history_id, str) or not 1 <= len(history_id) <= 128:
        raise RuntimeError("secret broker response contract is invalid")
    if (
        type(messages_total) is not int
        or type(threads_total) is not int
        or not 0 <= messages_total <= 2**63 - 1
        or not 0 <= threads_total <= 2**63 - 1
    ):
        raise RuntimeError("secret broker response contract is invalid")
    return GmailProfile(history_id, messages_total, threads_total)


def _close(response: Any) -> None:
    try:
        response.close()
    except Exception:
        pass
