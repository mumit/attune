"""Workload-authenticated client for the private channel broker."""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import UUID


class ChannelBrokerClient:
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
        if not 1 <= timeout_seconds <= 15:
            raise ValueError("channel broker timeout must be between 1 and 15 seconds")
        self._token_provider = token_provider or _google_id_token
        if session is None:
            import requests

            session = requests.Session()
            session.trust_env = False
        self._session = session
        self._timeout = timeout_seconds

    def link_google_chat_owner_dm(
        self,
        *,
        link_code: str,
        app_ref: str,
        actor_ref: str,
        destination_ref: str,
    ) -> bool:
        token = self._token_provider(self._audience)
        if not isinstance(token, str) or not token:
            raise RuntimeError("channel broker identity token is unavailable")
        response = self._session.post(
            f"{self._service_url}/v1/google-chat/link-owner-dm",
            json={
                "version": 1,
                "link_code": link_code,
                "app_ref": app_ref,
                "actor_ref": actor_ref,
                "destination_ref": destination_ref,
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._timeout,
            allow_redirects=False,
        )
        if response.status_code != 200:
            return False
        try:
            body = response.json()
        except ValueError:
            return False
        return body == {"status": "linked", "destination_status": "pending_test"}

    def test_google_chat_delivery(self, *, destination_id: UUID) -> bool:
        if not isinstance(destination_id, UUID):
            raise TypeError("destination_id must be a UUID")
        token = self._token_provider(self._audience)
        if not isinstance(token, str) or not token:
            raise RuntimeError("channel broker identity token is unavailable")
        response = self._session.post(
            f"{self._service_url}/v1/google-chat/test-delivery",
            json={"version": 1, "destination_id": str(destination_id)},
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._timeout,
            allow_redirects=False,
        )
        if response.status_code != 200:
            return False
        try:
            body = response.json()
        except ValueError:
            return False
        return body == {"status": "delivered", "destination_status": "active"}

    def accept_google_chat_message(
        self, *, app_ref: str, actor_ref: str, destination_ref: str,
        message_ref: str, text: str,
    ) -> UUID:
        token = self._token_provider(self._audience)
        if not isinstance(token, str) or not token:
            raise RuntimeError("channel broker identity token is unavailable")
        response = self._session.post(
            f"{self._service_url}/v1/google-chat/accept-message",
            json={
                "version": 1, "app_ref": app_ref, "actor_ref": actor_ref,
                "destination_ref": destination_ref, "message_ref": message_ref,
                "text": text,
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._timeout,
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise RuntimeError("channel message was not accepted")
        try:
            body = response.json()
            intent_id = UUID(body["dispatch_intent_id"])
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("channel message acceptance is invalid") from error
        if (
            not isinstance(body, dict)
            or set(body) != {"status", "dispatch_intent_id", "accepted_new"}
            or body["status"] != "accepted"
            or not isinstance(body["accepted_new"], bool)
        ):
            raise RuntimeError("channel message acceptance is invalid")
        return intent_id

    def install_slack(
        self, *, state: str, code: str, tenant_id: UUID, principal_id: UUID
    ) -> bool:
        if not isinstance(tenant_id, UUID) or not isinstance(principal_id, UUID):
            raise TypeError("install identifiers must be UUIDs")
        token = self._token_provider(self._audience)
        if not isinstance(token, str) or not token:
            raise RuntimeError("channel broker identity token is unavailable")
        response = self._session.post(
            f"{self._service_url}/v1/slack/install",
            json={
                "version": 1,
                "state": state,
                "code": code,
                "tenant_id": str(tenant_id),
                "principal_id": str(principal_id),
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._timeout,
            allow_redirects=False,
        )
        if response.status_code != 200:
            return False
        try:
            body = response.json()
        except ValueError:
            return False
        return body == {"status": "installed", "destination_status": "pending_test"}

    def test_slack_delivery(self, *, destination_id: UUID) -> bool:
        if not isinstance(destination_id, UUID):
            raise TypeError("destination_id must be a UUID")
        token = self._token_provider(self._audience)
        if not isinstance(token, str) or not token:
            raise RuntimeError("channel broker identity token is unavailable")
        response = self._session.post(
            f"{self._service_url}/v1/slack/test-delivery",
            json={"version": 1, "destination_id": str(destination_id)},
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._timeout,
            allow_redirects=False,
        )
        if response.status_code != 200:
            return False
        try:
            body = response.json()
        except ValueError:
            return False
        return body == {"status": "delivered", "destination_status": "active"}

    def accept_slack_message(
        self, *, team_ref: str, actor_ref: str, destination_ref: str,
        message_ref: str, text: str,
    ) -> UUID:
        token = self._token_provider(self._audience)
        if not isinstance(token, str) or not token:
            raise RuntimeError("channel broker identity token is unavailable")
        response = self._session.post(
            f"{self._service_url}/v1/slack/accept-message",
            json={
                "version": 1, "team_ref": team_ref, "actor_ref": actor_ref,
                "destination_ref": destination_ref, "message_ref": message_ref,
                "text": text,
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._timeout,
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise RuntimeError("channel message was not accepted")
        try:
            body = response.json()
            intent_id = UUID(body["dispatch_intent_id"])
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("channel message acceptance is invalid") from error
        if (
            not isinstance(body, dict)
            or set(body) != {"status", "dispatch_intent_id", "accepted_new"}
            or body["status"] != "accepted"
            or not isinstance(body["accepted_new"], bool)
        ):
            raise RuntimeError("channel message acceptance is invalid")
        return intent_id

    def acknowledge_slack_message(
        self, *, team_ref: str, actor_ref: str, destination_ref: str,
        message_ref: str,
    ) -> bool:
        token = self._token_provider(self._audience)
        if not isinstance(token, str) or not token:
            raise RuntimeError("channel broker identity token is unavailable")
        response = self._session.post(
            f"{self._service_url}/v1/slack/acknowledge",
            json={
                "version": 1, "team_ref": team_ref, "actor_ref": actor_ref,
                "destination_ref": destination_ref, "message_ref": message_ref,
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._timeout,
            allow_redirects=False,
        )
        if response.status_code != 200:
            return False
        try:
            return response.json() == {"status": "acknowledged"}
        except ValueError:
            return False

    def deliver_slack_reply(self, *, destination_id: UUID, job_id: UUID) -> bool:
        if not isinstance(destination_id, UUID) or not isinstance(job_id, UUID):
            raise TypeError("delivery references must be UUIDs")
        token = self._token_provider(self._audience)
        if not isinstance(token, str) or not token:
            raise RuntimeError("channel broker identity token is unavailable")
        response = self._session.post(
            f"{self._service_url}/v1/slack/deliver-reply",
            json={
                "version": 1,
                "destination_id": str(destination_id),
                "job_id": str(job_id),
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._timeout,
            allow_redirects=False,
        )
        if response.status_code != 200:
            return False
        try:
            return response.json() == {"status": "delivered"}
        except ValueError:
            return False

    def deliver_google_chat_reply(
        self, *, destination_id: UUID, job_id: UUID
    ) -> bool:
        if not isinstance(destination_id, UUID) or not isinstance(job_id, UUID):
            raise TypeError("delivery references must be UUIDs")
        token = self._token_provider(self._audience)
        if not isinstance(token, str) or not token:
            raise RuntimeError("channel broker identity token is unavailable")
        response = self._session.post(
            f"{self._service_url}/v1/google-chat/deliver-reply",
            json={"version": 1, "destination_id": str(destination_id), "job_id": str(job_id)},
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._timeout,
            allow_redirects=False,
        )
        if response.status_code != 200:
            return False
        try:
            return response.json() == {"status": "delivered"}
        except ValueError:
            return False

    def deliver_google_chat_brief(
        self, *, destination_id: UUID, job_id: UUID
    ) -> bool:
        """Hosted proactive brief delivery (docs/future-state.md Phase 5 item
        4, G12) -- mirrors :meth:`deliver_google_chat_reply` exactly."""
        if not isinstance(destination_id, UUID) or not isinstance(job_id, UUID):
            raise TypeError("delivery references must be UUIDs")
        token = self._token_provider(self._audience)
        if not isinstance(token, str) or not token:
            raise RuntimeError("channel broker identity token is unavailable")
        response = self._session.post(
            f"{self._service_url}/v1/google-chat/deliver-brief",
            json={"version": 1, "destination_id": str(destination_id), "job_id": str(job_id)},
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._timeout,
            allow_redirects=False,
        )
        if response.status_code != 200:
            return False
        try:
            return response.json() == {"status": "delivered"}
        except ValueError:
            return False

    def deliver_slack_brief(self, *, destination_id: UUID, job_id: UUID) -> bool:
        """Hosted proactive brief delivery (docs/future-state.md Phase 5 item
        4, G12) -- mirrors :meth:`deliver_slack_reply` exactly."""
        if not isinstance(destination_id, UUID) or not isinstance(job_id, UUID):
            raise TypeError("delivery references must be UUIDs")
        token = self._token_provider(self._audience)
        if not isinstance(token, str) or not token:
            raise RuntimeError("channel broker identity token is unavailable")
        response = self._session.post(
            f"{self._service_url}/v1/slack/deliver-brief",
            json={
                "version": 1,
                "destination_id": str(destination_id),
                "job_id": str(job_id),
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._timeout,
            allow_redirects=False,
        )
        if response.status_code != 200:
            return False
        try:
            return response.json() == {"status": "delivered"}
        except ValueError:
            return False


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
        raise ValueError("channel broker endpoint must be an HTTPS origin")
    return value.rstrip("/")


def _google_id_token(audience: str) -> str:
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    return id_token.fetch_id_token(Request(), audience)
