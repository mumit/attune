"""Fixed outbound Google Chat operation for hosted destination verification."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

CHAT_BOT_SCOPE = "https://www.googleapis.com/auth/chat.bot"
CHAT_MESSAGES_URL = "https://chat.googleapis.com/v1/{space}/messages"
CONNECTION_TEST_TEXT = "Attune connection test succeeded. No workspace data was accessed."
MAX_CHAT_TEXT_CHARS = 8_000
_SPACE = re.compile(r"^spaces/[A-Za-z0-9_-]{1,180}$")


class GoogleChatProviderFailure(RuntimeError):
    """Content-free provider failure safe for broker control flow."""


class GoogleChatProvider:
    def __init__(self, *, credentials: Any | None = None, session: Any | None = None):
        if credentials is None:
            import google.auth

            credentials, _ = google.auth.default(scopes=[CHAT_BOT_SCOPE])
        if session is None:
            from google.auth.transport.requests import AuthorizedSession

            session = AuthorizedSession(credentials)
            session.trust_env = False
        self._session = session

    def send_connection_test(self, *, space: str, request_id: UUID) -> None:
        self.send_message(space=space, text=CONNECTION_TEST_TEXT, request_id=request_id)

    def send_message(self, *, space: str, text: str, request_id: UUID) -> str:
        if not isinstance(space, str) or not _SPACE.fullmatch(space):
            raise ValueError("invalid Google Chat space")
        if not isinstance(text, str) or not 1 <= len(text) <= MAX_CHAT_TEXT_CHARS:
            raise ValueError("invalid Google Chat message")
        if not isinstance(request_id, UUID):
            raise TypeError("request_id must be a UUID")
        try:
            response = self._session.post(
                CHAT_MESSAGES_URL.format(space=space),
                params={"requestId": str(request_id)},
                json={"text": text},
                headers={"Accept": "application/json"},
                timeout=(3.05, 10),
                allow_redirects=False,
            )
        except Exception as error:
            raise GoogleChatProviderFailure("Google Chat delivery failed") from error
        if response.status_code != 200:
            raise GoogleChatProviderFailure("Google Chat delivery failed")
        try:
            body = response.json()
        except ValueError as error:
            raise GoogleChatProviderFailure("Google Chat response was invalid") from error
        name = body.get("name") if isinstance(body, dict) else None
        if not isinstance(name, str) or not name.startswith(f"{space}/messages/"):
            raise GoogleChatProviderFailure("Google Chat response was invalid")
        return name
