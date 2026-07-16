from attune.hosted.channel_broker_client import ChannelBrokerClient

URL = "https://channel-broker.example.run.app"
AUDIENCE = "https://channel-broker.attune.internal"


class Response:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {"status": "linked", "destination_status": "pending_test"}

    def json(self):
        return self._body


class Session:
    def __init__(self, response=None):
        self.response = response or Response()
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_client_uses_exact_audience_contract_and_disables_redirects():
    session = Session()
    client = ChannelBrokerClient(
        URL, AUDIENCE, token_provider=lambda audience: f"token:{audience}", session=session
    )
    assert client.link_google_chat_owner_dm(
        link_code="A" * 43,
        app_ref="projects/624765747204",
        actor_ref="users/123456",
        destination_ref="spaces/AAAA-test",
    )
    url, call = session.calls[0]
    assert url == f"{URL}/v1/google-chat/link-owner-dm"
    assert call["headers"] == {"Authorization": f"Bearer token:{AUDIENCE}"}
    assert call["allow_redirects"] is False
    assert set(call["json"]) == {
        "version", "link_code", "app_ref", "actor_ref", "destination_ref"
    }


def test_client_rejects_non_origin_urls_and_nonexact_success():
    for value in ("http://broker.example", "https://broker.example/path", "https://u:p@broker.example"):
        try:
            ChannelBrokerClient(value, AUDIENCE)
        except ValueError:
            pass
        else:
            raise AssertionError(value)
    client = ChannelBrokerClient(
        URL,
        AUDIENCE,
        token_provider=lambda _: "token",
        session=Session(Response(body={"status": "linked"})),
    )
    assert not client.link_google_chat_owner_dm(
        link_code="A" * 43,
        app_ref="projects/624765747204",
        actor_ref="users/123456",
        destination_ref="spaces/AAAA-test",
    )


def test_client_delivery_sends_only_canonical_destination_binding():
    destination = __import__("uuid").UUID("10000000-0000-4000-8000-000000000107")
    session = Session(Response(body={"status": "delivered", "destination_status": "active"}))
    client = ChannelBrokerClient(
        URL, AUDIENCE, token_provider=lambda _: "token", session=session
    )
    assert client.test_google_chat_delivery(destination_id=destination)
    url, call = session.calls[0]
    assert url == f"{URL}/v1/google-chat/test-delivery"
    assert call["json"] == {"version": 1, "destination_id": str(destination)}
