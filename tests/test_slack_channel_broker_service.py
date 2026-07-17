import time
from types import SimpleNamespace
from uuid import UUID

import pytest

from attune.hosted.channel_broker_client import ChannelBrokerClient
from attune.hosted.channel_broker_service import create_app

AUDIENCE = "https://channel-broker.attune.internal"
INGRESS = "ingress@example.iam.gserviceaccount.com"
CONTROL_PLANE = "control@example.iam.gserviceaccount.com"
WORKER = "worker@example.iam.gserviceaccount.com"
SLACK_INGRESS = "slack-ingress@example.iam.gserviceaccount.com"
URL = "https://channel-broker.example.run.app"
TENANT = "10000000-0000-4000-8000-000000000104"
PRINCIPAL = "10000000-0000-4000-8000-000000000105"
INSTALL_BODY = {
    "version": 1,
    "state": "A" * 43,
    "code": "code-123",
    "tenant_id": TENANT,
    "principal_id": PRINCIPAL,
}
MESSAGE_BODY = {
    "version": 1,
    "team_ref": "teams/T0123456789",
    "actor_ref": "teams/T0123456789/users/U0123456789",
    "destination_ref": "teams/T0123456789/channels/D0123456789",
    "message_ref": "teams/T0123456789/channels/D0123456789/messages/1752600000.000100",
    "text": "hello",
}


class GoogleChatBroker:
    def link_owner_dm(self, **kwargs):
        return SimpleNamespace(destination_status="pending_test")


class SlackBroker:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def install(self, **kwargs):
        self.calls.append(("install", kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(destination_status="pending_test")

    def test_delivery(self, **kwargs):
        self.calls.append(("test", kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(destination_status="active")

    def accept_message(self, **kwargs):
        self.calls.append(("accept", kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(
            dispatch_intent_id=UUID("10000000-0000-4000-8000-000000000111"),
            accepted_new=True,
        )

    def deliver_reply(self, **kwargs):
        self.calls.append(("reply", kwargs))
        if self.error:
            raise self.error
        return True


def claims(token, audience):
    now = int(time.time())
    return {
        "iss": "https://accounts.google.com",
        "aud": audience,
        "email": {
            "ingress": INGRESS,
            "control": CONTROL_PLANE,
            "worker": WORKER,
            "slack-ingress": SLACK_INGRESS,
        }.get(token, "other@example.iam.gserviceaccount.com"),
        "email_verified": True,
        "sub": token,
        "iat": now - 10,
        "exp": now + 300,
    }


def client(slack_broker):
    return create_app(
        GoogleChatBroker(),
        expected_audience=AUDIENCE,
        expected_ingress=INGRESS,
        expected_control_plane=CONTROL_PLANE,
        expected_worker=WORKER,
        slack_broker=slack_broker,
        expected_slack_ingress=SLACK_INGRESS,
        token_verifier=claims,
    ).test_client()


def test_slack_configuration_requires_paired_broker_and_distinct_identity():
    with pytest.raises(ValueError, match="together"):
        create_app(
            GoogleChatBroker(),
            expected_audience=AUDIENCE,
            expected_ingress=INGRESS,
            expected_control_plane=CONTROL_PLANE,
            expected_worker=WORKER,
            slack_broker=SlackBroker(),
            token_verifier=claims,
        )
    with pytest.raises(ValueError, match="distinct"):
        create_app(
            GoogleChatBroker(),
            expected_audience=AUDIENCE,
            expected_ingress=INGRESS,
            expected_control_plane=CONTROL_PLANE,
            expected_worker=WORKER,
            slack_broker=SlackBroker(),
            expected_slack_ingress=INGRESS,
            token_verifier=claims,
        )


def test_slack_routes_are_absent_when_slack_broker_is_not_configured():
    app = create_app(
        GoogleChatBroker(),
        expected_audience=AUDIENCE,
        expected_ingress=INGRESS,
        expected_control_plane=CONTROL_PLANE,
        expected_worker=WORKER,
        token_verifier=claims,
    ).test_client()
    assert app.post(
        "/v1/slack/install",
        headers={"Authorization": "Bearer control"},
        json=INSTALL_BODY,
    ).status_code == 404


def test_slack_install_is_control_plane_only_with_exact_body():
    broker = SlackBroker()
    app = client(broker)
    assert app.post(
        "/v1/slack/install",
        headers={"Authorization": "Bearer ingress"},
        json=INSTALL_BODY,
    ).status_code == 403
    assert app.post(
        "/v1/slack/install",
        headers={"Authorization": "Bearer control"},
        json={**INSTALL_BODY, "destination_id": "attacker"},
    ).status_code == 400
    assert app.post(
        "/v1/slack/install",
        headers={"Authorization": "Bearer control"},
        json={**INSTALL_BODY, "tenant_id": "not-a-uuid"},
    ).status_code == 400
    response = app.post(
        "/v1/slack/install",
        headers={"Authorization": "Bearer control"},
        json=INSTALL_BODY,
    )
    assert response.status_code == 200
    assert response.get_json() == {
        "status": "installed",
        "destination_status": "pending_test",
    }
    kind, call = broker.calls[0]
    assert kind == "install"
    assert call["owner_tenant_id"] == UUID(TENANT)
    assert call["owner_principal_id"] == UUID(PRINCIPAL)


def test_slack_message_acceptance_is_slack_ingress_only():
    broker = SlackBroker()
    app = client(broker)
    for token in ("ingress", "control", "worker", "other"):
        assert app.post(
            "/v1/slack/accept-message",
            headers={"Authorization": f"Bearer {token}"},
            json=MESSAGE_BODY,
        ).status_code == 403
    response = app.post(
        "/v1/slack/accept-message",
        headers={"Authorization": "Bearer slack-ingress"},
        json=MESSAGE_BODY,
    )
    assert response.get_json() == {
        "status": "accepted",
        "dispatch_intent_id": "10000000-0000-4000-8000-000000000111",
        "accepted_new": True,
    }


def test_slack_delivery_and_reply_split_control_plane_and_worker_identities():
    broker = SlackBroker()
    app = client(broker)
    delivery_body = {"version": 1, "destination_id": "10000000-0000-4000-8000-000000000107"}
    assert app.post(
        "/v1/slack/test-delivery",
        headers={"Authorization": "Bearer worker"},
        json=delivery_body,
    ).status_code == 403
    assert app.post(
        "/v1/slack/test-delivery",
        headers={"Authorization": "Bearer control"},
        json=delivery_body,
    ).get_json() == {"status": "delivered", "destination_status": "active"}
    reply_body = {
        "version": 1,
        "destination_id": "10000000-0000-4000-8000-000000000107",
        "job_id": "10000000-0000-4000-8000-000000000112",
    }
    assert app.post(
        "/v1/slack/deliver-reply",
        headers={"Authorization": "Bearer control"},
        json=reply_body,
    ).status_code == 403
    assert app.post(
        "/v1/slack/deliver-reply",
        headers={"Authorization": "Bearer worker"},
        json=reply_body,
    ).get_json() == {"status": "delivered"}
    assert app.post(
        "/v1/slack/deliver-reply",
        headers={"Authorization": "Bearer worker"},
        json={**reply_body, "text": "attacker supplied"},
    ).status_code == 400


def test_slack_failures_are_content_free():
    app = client(SlackBroker(RuntimeError("xoxb-secret-token")))
    response = app.post(
        "/v1/slack/install",
        headers={"Authorization": "Bearer control"},
        json=INSTALL_BODY,
    )
    assert response.status_code == 503
    assert b"xoxb" not in response.data


def test_client_slack_methods_use_exact_versioned_contracts():
    class Response:
        def __init__(self, body):
            self.status_code = 200
            self._body = body

        def json(self):
            return self._body

    class Session:
        def __init__(self, body):
            self.body = body
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response(self.body)

    session = Session({"status": "installed", "destination_status": "pending_test"})
    broker_client = ChannelBrokerClient(
        URL, AUDIENCE, token_provider=lambda _: "token", session=session
    )
    assert broker_client.install_slack(
        state="A" * 43, code="code-123",
        tenant_id=UUID(TENANT), principal_id=UUID(PRINCIPAL),
    )
    url, call = session.calls[0]
    assert url == f"{URL}/v1/slack/install"
    assert set(call["json"]) == {"version", "state", "code", "tenant_id", "principal_id"}
    assert call["allow_redirects"] is False

    session = Session({"status": "delivered", "destination_status": "active"})
    broker_client = ChannelBrokerClient(
        URL, AUDIENCE, token_provider=lambda _: "token", session=session
    )
    assert broker_client.test_slack_delivery(
        destination_id=UUID("10000000-0000-4000-8000-000000000107")
    )
    assert session.calls[0][0] == f"{URL}/v1/slack/test-delivery"

    session = Session({
        "status": "accepted",
        "dispatch_intent_id": "10000000-0000-4000-8000-000000000111",
        "accepted_new": True,
    })
    broker_client = ChannelBrokerClient(
        URL, AUDIENCE, token_provider=lambda _: "token", session=session
    )
    assert broker_client.accept_slack_message(
        team_ref=MESSAGE_BODY["team_ref"],
        actor_ref=MESSAGE_BODY["actor_ref"],
        destination_ref=MESSAGE_BODY["destination_ref"],
        message_ref=MESSAGE_BODY["message_ref"],
        text="hello",
    ) == UUID("10000000-0000-4000-8000-000000000111")

    session = Session({"status": "delivered"})
    broker_client = ChannelBrokerClient(
        URL, AUDIENCE, token_provider=lambda _: "token", session=session
    )
    assert broker_client.deliver_slack_reply(
        destination_id=UUID("10000000-0000-4000-8000-000000000107"),
        job_id=UUID("10000000-0000-4000-8000-000000000112"),
    )
    assert session.calls[0][0] == f"{URL}/v1/slack/deliver-reply"
