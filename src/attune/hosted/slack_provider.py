"""Fixed outbound Slack operations for hosted installation and delivery."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

OAUTH_ACCESS_URL = "https://slack.com/api/oauth.v2.access"
CONVERSATIONS_OPEN_URL = "https://slack.com/api/conversations.open"
POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
REQUIRED_BOT_SCOPES = frozenset({"chat:write", "im:write", "im:history"})
CONNECTION_TEST_TEXT = "Attune connection test succeeded. No workspace data was accessed."
MAX_SLACK_TEXT_CHARS = 8_000
TEAM_ID = re.compile(r"^T[A-Z0-9]{4,20}$")
USER_ID = re.compile(r"^[UW][A-Z0-9]{4,20}$")
IM_CHANNEL_ID = re.compile(r"^D[A-Z0-9]{4,20}$")
MESSAGE_TS = re.compile(r"^[0-9]{6,20}\.[0-9]{1,10}$")
_BOT_TOKEN = re.compile(r"^xoxb-[A-Za-z0-9-]{10,240}$")
_APP_ID = re.compile(r"^A[A-Z0-9]{4,20}$")


class SlackProviderFailure(RuntimeError):
    """Content-free provider failure safe for broker control flow."""


@dataclass(frozen=True, repr=False)
class SlackInstallation:
    app_id: str
    team_id: str
    installer_user_id: str
    bot_user_id: str
    bot_token: str

    def __repr__(self) -> str:
        return "SlackInstallation(app_id=<redacted>, team_id=<redacted>, installer_user_id=<redacted>, bot_user_id=<redacted>, bot_token=<redacted>)"


def validate_bot_token(token: object) -> str:
    if not isinstance(token, str) or not _BOT_TOKEN.fullmatch(token):
        raise SlackProviderFailure("Slack bot token is invalid")
    return token


class SlackProvider:
    """Owns the fixed Slack API URLs and the platform client credential.

    The client secret is supplied only to the private channel broker's
    composition root; ingress and the control plane never construct this
    class with a secret.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        expected_app_id: str,
        session: Any | None = None,
    ):
        if not isinstance(client_id, str) or not 1 <= len(client_id) <= 64:
            raise ValueError("Slack client id is invalid")
        if not isinstance(client_secret, str) or not 8 <= len(client_secret) <= 128:
            raise ValueError("Slack client secret is invalid")
        if not isinstance(expected_app_id, str) or not _APP_ID.fullmatch(expected_app_id):
            raise ValueError("Slack app id is invalid")
        self._client_id = client_id
        self._client_secret = client_secret
        self._expected_app_id = expected_app_id
        if session is None:
            import requests

            session = requests.Session()
            session.trust_env = False
        self._session = session

    def exchange_code(self, *, code: str, redirect_uri: str) -> SlackInstallation:
        if not isinstance(code, str) or not 1 <= len(code) <= 512:
            raise ValueError("Slack authorization code is invalid")
        if not isinstance(redirect_uri, str) or not redirect_uri.startswith("https://"):
            raise ValueError("Slack redirect URI must be HTTPS")
        body = self._call(
            OAUTH_ACCESS_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        app_id = body.get("app_id")
        team = body.get("team")
        authed_user = body.get("authed_user")
        token_type = body.get("token_type")
        scope = body.get("scope")
        bot_user_id = body.get("bot_user_id")
        access_token = body.get("access_token")
        if (
            app_id != self._expected_app_id
            or token_type != "bot"
            or not isinstance(team, dict)
            or not isinstance(authed_user, dict)
            or not isinstance(scope, str)
        ):
            raise SlackProviderFailure("Slack installation response was invalid")
        granted = frozenset(part for part in scope.split(",") if part)
        if granted != REQUIRED_BOT_SCOPES:
            raise SlackProviderFailure("Slack installation scopes do not match")
        team_id = team.get("id")
        installer = authed_user.get("id")
        if (
            not isinstance(team_id, str) or not TEAM_ID.fullmatch(team_id)
            or not isinstance(installer, str) or not USER_ID.fullmatch(installer)
            or not isinstance(bot_user_id, str) or not USER_ID.fullmatch(bot_user_id)
        ):
            raise SlackProviderFailure("Slack installation response was invalid")
        # A user token must never be retained; refuse a response carrying one.
        if authed_user.get("access_token") is not None:
            raise SlackProviderFailure("Slack installation returned a user token")
        return SlackInstallation(
            app_id, team_id, installer, bot_user_id, validate_bot_token(access_token)
        )

    def open_owner_dm(self, *, bot_token: str, user_id: str) -> str:
        validate_bot_token(bot_token)
        if not isinstance(user_id, str) or not USER_ID.fullmatch(user_id):
            raise ValueError("Slack user id is invalid")
        body = self._call(
            CONVERSATIONS_OPEN_URL,
            json={"users": user_id},
            bot_token=bot_token,
        )
        channel = body.get("channel")
        channel_id = channel.get("id") if isinstance(channel, dict) else None
        if not isinstance(channel_id, str) or not IM_CHANNEL_ID.fullmatch(channel_id):
            raise SlackProviderFailure("Slack direct message resolution failed")
        return channel_id

    def send_connection_test(self, *, bot_token: str, channel: str) -> str:
        return self.send_message(
            bot_token=bot_token, channel=channel, text=CONNECTION_TEST_TEXT
        )

    def send_message(
        self, *, bot_token: str, channel: str, text: str,
        request_id: UUID | None = None,
    ) -> str:
        validate_bot_token(bot_token)
        if not isinstance(channel, str) or not IM_CHANNEL_ID.fullmatch(channel):
            raise ValueError("invalid Slack channel")
        if not isinstance(text, str) or not 1 <= len(text) <= MAX_SLACK_TEXT_CHARS:
            raise ValueError("invalid Slack message")
        body = self._call(
            POST_MESSAGE_URL,
            json={"channel": channel, "text": text},
            bot_token=bot_token,
        )
        posted_channel = body.get("channel")
        posted_ts = body.get("ts")
        if (
            posted_channel != channel
            or not isinstance(posted_ts, str)
            or not MESSAGE_TS.fullmatch(posted_ts)
        ):
            raise SlackProviderFailure("Slack response was invalid")
        return posted_ts

    def _call(
        self, url: str, *, data: dict | None = None, json: dict | None = None,
        bot_token: str | None = None,
    ) -> dict:
        headers = {"Accept": "application/json"}
        if bot_token is not None:
            headers["Authorization"] = f"Bearer {bot_token}"
        try:
            response = self._session.post(
                url,
                data=data,
                json=json,
                headers=headers,
                timeout=(3.05, 10),
                allow_redirects=False,
            )
        except Exception as error:
            raise SlackProviderFailure("Slack request failed") from error
        if response.status_code != 200:
            raise SlackProviderFailure("Slack request failed")
        try:
            body = response.json()
        except ValueError as error:
            raise SlackProviderFailure("Slack response was invalid") from error
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise SlackProviderFailure("Slack request was refused")
        return body


def build_authorize_url(*, client_id: str, state: str, redirect_uri: str) -> str:
    """Fixed Slack authorize URL for the control plane; carries no secret."""
    from urllib.parse import urlencode

    if not isinstance(client_id, str) or not 1 <= len(client_id) <= 64:
        raise ValueError("Slack client id is invalid")
    if not isinstance(state, str) or not 20 <= len(state) <= 128:
        raise ValueError("Slack OAuth state is invalid")
    if not isinstance(redirect_uri, str) or not redirect_uri.startswith("https://"):
        raise ValueError("Slack redirect URI must be HTTPS")
    query = urlencode(
        {
            "client_id": client_id,
            "scope": ",".join(sorted(REQUIRED_BOT_SCOPES)),
            "user_scope": "",
            "state": state,
            "redirect_uri": redirect_uri,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"
