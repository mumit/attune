from uuid import UUID

import pytest

from attune.hosted.google_chat_provider import (
    CHAT_MESSAGES_URL,
    CONNECTION_TEST_TEXT,
    GoogleChatProvider,
    GoogleChatProviderFailure,
)


class Response:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {"name": "spaces/AAAA-test/messages/BBBB"}

    def json(self):
        return self._body


class Session:
    def __init__(self, response=None):
        self.response = response or Response()
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_provider_sends_only_fixed_text_to_canonical_chat_endpoint():
    session = Session()
    provider = GoogleChatProvider(credentials=object(), session=session)
    request_id = UUID("10000000-0000-4000-8000-000000000109")
    provider.send_connection_test(space="spaces/AAAA-test", request_id=request_id)
    url, call = session.calls[0]
    assert url == CHAT_MESSAGES_URL.format(space="spaces/AAAA-test")
    assert call["json"] == {"text": CONNECTION_TEST_TEXT}
    assert call["params"] == {"requestId": str(request_id)}
    assert call["allow_redirects"] is False


@pytest.mark.parametrize(
    "response",
    [Response(403), Response(body={"name": "spaces/other/messages/BBBB"})],
)
def test_provider_fails_closed_on_refusal_or_wrong_resource(response):
    with pytest.raises(GoogleChatProviderFailure):
        GoogleChatProvider(credentials=object(), session=Session(response)).send_connection_test(
            space="spaces/AAAA-test",
            request_id=UUID("10000000-0000-4000-8000-000000000109"),
        )
