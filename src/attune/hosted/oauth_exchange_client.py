"""Credential-free callback client for the private OAuth exchange service."""

from __future__ import annotations

from typing import Callable
from urllib.parse import urlsplit


class PrivateOAuthExchangeClient:
    def __init__(
        self,
        service_url: str,
        audience: str,
        *,
        session=None,
        token_provider: Callable[[str], str] | None = None,
    ):
        self._service_url = _https_origin(service_url)
        self._audience = _https_origin(audience)
        if session is None:
            import requests

            session = requests.Session()
            session.trust_env = False
        self._session = session
        self._token_provider = token_provider or _identity_token

    def exchange(self, *, code: str, state: str, binding: str) -> bool:
        token = self._token_provider(self._audience)
        response = self._session.post(
            f"{self._service_url}/v1/oauth/google/exchange",
            json={"code": code, "state": state, "binding": binding},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=10,
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
        raise ValueError("private OAuth exchange endpoint must be an HTTPS origin")
    return value.rstrip("/")


def _identity_token(audience: str) -> str:
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    return id_token.fetch_id_token(Request(), audience)
