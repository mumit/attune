import time
from types import SimpleNamespace
from uuid import UUID

from attune.hosted.channel_broker_service import create_app

AUDIENCE = "https://channel-broker.attune.internal"
INGRESS = "ingress@example.iam.gserviceaccount.com"
CONTROL_PLANE = "control@example.iam.gserviceaccount.com"
WORKER = "worker@example.iam.gserviceaccount.com"
BODY = {
    "version": 1,
    "link_code": "A" * 43,
    "app_ref": "projects/624765747204",
    "actor_ref": "users/123456",
    "destination_ref": "spaces/AAAA-test",
}


class Broker:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def link_owner_dm(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(destination_status="pending_test")

    def test_delivery(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(destination_status="active")

    def accept_message(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(
            dispatch_intent_id=UUID("10000000-0000-4000-8000-000000000111"),
            accepted_new=True,
        )

    def deliver_reply(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return True


def claims(token, audience):
    now = int(time.time())
    return {
        "iss": "https://accounts.google.com",
        "aud": audience,
        "email": (
            INGRESS if token == "ingress" else
            CONTROL_PLANE if token == "control" else
            WORKER if token == "worker" else
            "other@example.iam.gserviceaccount.com"
        ),
        "email_verified": True,
        "sub": token,
        "iat": now - 10,
        "exp": now + 300,
    }


def client(broker):
    return create_app(
        broker,
        expected_audience=AUDIENCE,
        expected_ingress=INGRESS,
        expected_control_plane=CONTROL_PLANE,
        expected_worker=WORKER,
        token_verifier=claims,
    ).test_client()


def test_private_service_accepts_only_ingress_identity_and_exact_body():
    broker = Broker()
    app = client(broker)
    assert app.post(
        "/v1/google-chat/link-owner-dm",
        headers={"Authorization": "Bearer other"},
        json=BODY,
    ).status_code == 403
    assert app.post(
        "/v1/google-chat/link-owner-dm",
        headers={"Authorization": "Bearer ingress"},
        json={**BODY, "tenant_id": "attacker-controlled"},
    ).status_code == 400
    response = app.post(
        "/v1/google-chat/link-owner-dm",
        headers={"Authorization": "Bearer ingress"},
        json=BODY,
    )
    assert response.status_code == 200
    assert response.get_json() == {
        "status": "linked",
        "destination_status": "pending_test",
    }
    assert set(broker.calls[0]) == {
        "link_code", "app_ref", "actor_ref", "destination_ref"
    }


def test_service_health_and_failures_are_content_free():
    app = client(Broker(RuntimeError("sensitive provider identifier")))
    assert app.get("/healthz").get_json() == {"status": "ok"}
    response = app.post(
        "/v1/google-chat/link-owner-dm",
        headers={"Authorization": "Bearer ingress"},
        json=BODY,
    )
    assert response.status_code == 503
    assert b"sensitive provider identifier" not in response.data


def test_delivery_accepts_only_control_plane_and_canonical_uuid():
    broker = Broker()
    app = client(broker)
    body = {"version": 1, "destination_id": "10000000-0000-4000-8000-000000000107"}
    assert app.post(
        "/v1/google-chat/test-delivery",
        headers={"Authorization": "Bearer ingress"},
        json=body,
    ).status_code == 403
    response = app.post(
        "/v1/google-chat/test-delivery",
        headers={"Authorization": "Bearer control"},
        json=body,
    )
    assert response.status_code == 200
    assert response.get_json() == {"status": "delivered", "destination_status": "active"}


def test_message_acceptance_is_ingress_only_and_rejects_tenant_authority():
    broker = Broker()
    body = {
        "version": 1,
        "app_ref": "projects/624765747204",
        "actor_ref": "users/123456",
        "destination_ref": "spaces/AAAA-test",
        "message_ref": "spaces/AAAA-test/messages/message-123",
        "text": "hello",
    }
    app = client(broker)
    assert app.post(
        "/v1/google-chat/accept-message",
        headers={"Authorization": "Bearer control"}, json=body,
    ).status_code == 403
    assert app.post(
        "/v1/google-chat/accept-message",
        headers={"Authorization": "Bearer ingress"},
        json={**body, "tenant_id": "attacker"},
    ).status_code == 400
    response = app.post(
        "/v1/google-chat/accept-message",
        headers={"Authorization": "Bearer ingress"}, json=body,
    )
    assert response.get_json() == {
        "status": "accepted",
        "dispatch_intent_id": "10000000-0000-4000-8000-000000000111",
        "accepted_new": True,
    }


def test_conversation_delivery_is_worker_only_and_accepts_no_content_or_tenant():
    broker = Broker()
    app = client(broker)
    body = {
        "version": 1,
        "destination_id": "10000000-0000-4000-8000-000000000107",
        "job_id": "10000000-0000-4000-8000-000000000108",
    }
    assert app.post(
        "/v1/google-chat/deliver-reply",
        headers={"Authorization": "Bearer ingress"}, json=body,
    ).status_code == 403
    response = app.post(
        "/v1/google-chat/deliver-reply",
        headers={"Authorization": "Bearer worker"}, json=body,
    )
    assert response.get_json() == {"status": "delivered"}
    assert set(broker.calls[-1]) == {"destination_id", "job_id"}
    assert app.post(
        "/v1/google-chat/deliver-reply",
        headers={"Authorization": "Bearer worker"},
        json={**body, "text": "attacker supplied", "tenant_id": "attacker"},
    ).status_code == 400
