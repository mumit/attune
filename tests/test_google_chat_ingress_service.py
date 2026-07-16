import time

from attune.hosted.google_chat_ingress_service import CHAT_CALLER, create_app
from test_google_chat_ingress import event

AUDIENCE = "https://dev.attune.example/v1/provider/google-chat/events"


class Broker:
    def __init__(self, result=True, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def link_google_chat_owner_dm(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


def claims(token, audience):
    now = int(time.time())
    return {
        "iss": "https://accounts.google.com",
        "aud": audience,
        "email": CHAT_CALLER if token == "chat" else "attacker@example.com",
        "email_verified": True,
        "sub": token,
        "iat": now - 10,
        "exp": now + 300,
    }


def client(broker):
    return create_app(
        broker,
        expected_audience=AUDIENCE,
        app_project_number="624765747204",
        token_verifier=claims,
    ).test_client()


def test_ingress_verifies_google_and_forwards_no_tenant_authority():
    broker = Broker()
    app = client(broker)
    assert app.post(
        "/v1/provider/google-chat/events",
        headers={"Authorization": "Bearer attacker"},
        json=event(),
    ).status_code == 403
    response = app.post(
        "/v1/provider/google-chat/events",
        headers={"Authorization": "Bearer chat"},
        json=event(),
    )
    assert response.status_code == 200
    assert response.get_json() == {
        "text": "Attune is connected. Return to the setup page to continue."
    }
    assert broker.calls == [{
        "link_code": "A" * 43,
        "app_ref": "projects/624765747204",
        "actor_ref": "users/123456",
        "destination_ref": "spaces/AAAA-test",
    }]


def test_non_link_and_broker_failure_are_content_bounded_and_generic():
    invalid = event()
    invalid["message"]["text"] = "hello"
    response = client(Broker()).post(
        "/v1/provider/google-chat/events",
        headers={"Authorization": "Bearer chat"},
        json=invalid,
    )
    assert response.status_code == 200
    assert "/link" in response.get_json()["text"]

    response = client(Broker(error=RuntimeError("sensitive user and space"))).post(
        "/v1/provider/google-chat/events",
        headers={"Authorization": "Bearer chat"},
        json=event(),
    )
    assert response.status_code == 200
    assert b"sensitive user and space" not in response.data
