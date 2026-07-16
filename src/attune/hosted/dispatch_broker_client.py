"""Authenticated intent-only client for the private dispatch broker."""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import UUID


class DispatchBrokerClient:
    def __init__(
        self,
        service_url: str,
        audience: str,
        *,
        token_provider: Callable[[str], str] | None = None,
        session: Any | None = None,
        timeout_seconds: float = 10.0,
    ):
        self._service_url = _https_origin(service_url)
        self._audience = _https_origin(audience)
        if not 1 <= timeout_seconds <= 30:
            raise ValueError("dispatch broker timeout must be between 1 and 30 seconds")
        self._token_provider = token_provider or _google_id_token
        if session is None:
            import requests

            session = requests.Session()
            session.trust_env = False
        self._session = session
        self._timeout = timeout_seconds

    def dispatch(self, intent_id: UUID) -> bool:
        if not isinstance(intent_id, UUID):
            raise TypeError("intent_id must be a UUID")
        token = self._token_provider(self._audience)
        if not isinstance(token, str) or not token:
            raise RuntimeError("dispatch broker identity token is unavailable")
        response = self._session.post(
            f"{self._service_url}/v1/dispatch-intents/dispatch",
            json={"intent_id": str(intent_id)},
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._timeout,
            allow_redirects=False,
        )
        return response.status_code == 204


def _https_origin(value: str) -> str:
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
        raise ValueError("dispatch broker endpoint must be an HTTPS origin")
    return value.rstrip("/")


def _google_id_token(audience: str) -> str:
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    return id_token.fetch_id_token(Request(), audience)
