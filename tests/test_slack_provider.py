import pytest

from attune.hosted.slack_provider import (
    CONNECTION_TEST_TEXT,
    SlackProvider,
    SlackProviderFailure,
    build_authorize_url,
)

CLIENT_ID = "1234567890.0987654321"
APP_ID = "A0123456789"


class Response:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def oauth_body(**overrides):
    body = {
        "ok": True,
        "app_id": APP_ID,
        "token_type": "bot",
        "scope": "chat:write,im:write,im:history",
        "access_token": "xoxb-1234567890-abcdefghij",
        "bot_user_id": "U0BOT456789",
        "team": {"id": "T0123456789", "name": "example"},
        "authed_user": {"id": "U0123456789"},
    }
    body.update(overrides)
    return body


def provider(responses):
    return SlackProvider(
        client_id=CLIENT_ID,
        client_secret="s" * 32,
        expected_app_id=APP_ID,
        session=Session(responses),
    )


def test_exchange_posts_fixed_url_and_returns_verified_installation():
    session = Session([Response(body=oauth_body())])
    slack = SlackProvider(
        client_id=CLIENT_ID, client_secret="s" * 32,
        expected_app_id=APP_ID, session=session,
    )
    installation = slack.exchange_code(
        code="code-123", redirect_uri="https://dev.attune.example/callback"
    )
    assert installation.team_id == "T0123456789"
    assert installation.installer_user_id == "U0123456789"
    assert installation.bot_token.startswith("xoxb-")
    url, call = session.calls[0]
    assert url == "https://slack.com/api/oauth.v2.access"
    assert call["allow_redirects"] is False
    assert call["data"]["code"] == "code-123"
    assert "xoxb" not in repr(installation)


@pytest.mark.parametrize(
    "overrides",
    [
        {"app_id": "A9999999999"},
        {"token_type": "user"},
        {"scope": "chat:write"},
        {"scope": "chat:write,im:write,im:history,files:read"},
        {"access_token": "xoxp-user-token-value"},
        {"bot_user_id": "not-a-user"},
        {"team": {"id": "bad"}},
        {"authed_user": {"id": "U0123456789", "access_token": "xoxp-secret"}},
        {"ok": False},
    ],
)
def test_exchange_refuses_noncanonical_installations(overrides):
    slack = provider([Response(body=oauth_body(**overrides))])
    with pytest.raises(SlackProviderFailure):
        slack.exchange_code(
            code="code-123", redirect_uri="https://dev.attune.example/callback"
        )


def test_open_owner_dm_returns_only_one_to_one_channel():
    slack = provider([Response(body={"ok": True, "channel": {"id": "D0123456789"}})])
    assert slack.open_owner_dm(
        bot_token="xoxb-1234567890-abcdefghij", user_id="U0123456789"
    ) == "D0123456789"
    slack = provider([Response(body={"ok": True, "channel": {"id": "C0123456789"}})])
    with pytest.raises(SlackProviderFailure):
        slack.open_owner_dm(
            bot_token="xoxb-1234567890-abcdefghij", user_id="U0123456789"
        )


def test_send_message_validates_channel_response_and_is_content_free_on_failure():
    slack = provider([
        Response(body={"ok": True, "channel": "D0123456789", "ts": "1752600000.000200"})
    ])
    assert slack.send_message(
        bot_token="xoxb-1234567890-abcdefghij",
        channel="D0123456789",
        text="hello",
    ) == "1752600000.000200"
    slack = provider([Response(status=500)])
    with pytest.raises(SlackProviderFailure):
        slack.send_message(
            bot_token="xoxb-1234567890-abcdefghij",
            channel="D0123456789",
            text="hello",
        )
    with pytest.raises(ValueError):
        provider([]).send_message(
            bot_token="xoxb-1234567890-abcdefghij",
            channel="C0123456789",
            text="hello",
        )


def test_connection_test_sends_the_immutable_sentence():
    session = Session([
        Response(body={"ok": True, "channel": "D0123456789", "ts": "1752600000.000300"})
    ])
    slack = SlackProvider(
        client_id=CLIENT_ID, client_secret="s" * 32,
        expected_app_id=APP_ID, session=session,
    )
    slack.send_connection_test(
        bot_token="xoxb-1234567890-abcdefghij", channel="D0123456789"
    )
    assert session.calls[0][1]["json"]["text"] == CONNECTION_TEST_TEXT


def test_authorize_url_is_fixed_and_carries_no_secret():
    url = build_authorize_url(
        client_id=CLIENT_ID,
        state="s" * 43,
        redirect_uri="https://dev.attune.example/v1/onboarding/channel-installations/slack/callback",
    )
    assert url.startswith("https://slack.com/oauth/v2/authorize?")
    assert "chat%3Awrite" in url and "im%3Awrite" in url and "im%3Ahistory" in url
    assert "secret" not in url
    with pytest.raises(ValueError):
        build_authorize_url(
            client_id=CLIENT_ID, state="short", redirect_uri="https://x.example"
        )
