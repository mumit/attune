"""Authenticated client for the private intent-only audit writer."""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import UUID


TokenProvider = Callable[[str], str]


class AuditWriterClient:
    """Write an opaque audit intent through an authenticated private service."""

    def __init__(
        self,
        service_url: str,
        *,
        token_provider: TokenProvider | None = None,
        session: Any | None = None,
        timeout_seconds: float = 10.0,
    ):
        parsed = urlsplit(service_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("audit writer URL must be an HTTPS origin")
        if not 1 <= timeout_seconds <= 30:
            raise ValueError("audit writer timeout must be between 1 and 30 seconds")
        self._service_url = service_url.rstrip("/")
        self._token_provider = token_provider or _google_id_token
        self._session = session
        self._timeout = timeout_seconds

    def write(self, audit_intent_id: UUID) -> bool:
        import requests

        if not isinstance(audit_intent_id, UUID):
            raise TypeError("audit_intent_id must be a UUID")
        token = self._token_provider(self._service_url)
        if not isinstance(token, str) or not token:
            raise RuntimeError("audit writer identity token is unavailable")
        session = self._session or requests
        response = session.post(
            f"{self._service_url}/v1/audit-intents/write",
            json={"audit_intent_id": str(audit_intent_id)},
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._timeout,
            allow_redirects=False,
        )
        if response.status_code != 200:
            return False
        try:
            body = response.json()
            event_id = UUID(body["audit_event_id"])
        except (KeyError, TypeError, ValueError):
            return False
        return (
            isinstance(body, dict)
            and set(body) == {"status", "audit_event_id"}
            and body["status"] == "written"
            and body["audit_event_id"] == str(event_id)
        )


def _google_id_token(audience: str) -> str:
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    return id_token.fetch_id_token(Request(), audience)
