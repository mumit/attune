from __future__ import annotations

import time
from types import SimpleNamespace
from uuid import UUID

import pytest

pytest.importorskip("flask")

from attune.hosted.secret_broker_service import create_app

INTENT = UUID("10000000-0000-4000-8000-000000000401")
AUDIENCE = "https://secret-broker.example.run.app"
CALLER = "control@example.iam.gserviceaccount.com"


class Broker:
    def __init__(self):
        self.calls = []

    def install(self, intent_id, credential):
        self.calls.append(("install", intent_id, credential))
        return SimpleNamespace(status_code=204)

    def revoke(self, intent_id):
        self.calls.append(("revoke", intent_id))
        return SimpleNamespace(status_code=204)


class FailingBroker(Broker):
    def install(self, intent_id, credential):
        raise RuntimeError("secret detail")


def claims(token, audience):
    assert token == "valid" and audience == AUDIENCE
    now = int(time.time())
    return {
        "iss": "https://accounts.google.com",
        "aud": AUDIENCE,
        "email": CALLER,
        "email_verified": True,
        "sub": "123",
        "iat": now - 10,
        "exp": now + 300,
    }


def client(broker):
    return create_app(
        broker,
        expected_audience=AUDIENCE,
        expected_control_plane=CALLER,
        token_verifier=claims,
    ).test_client()


def test_install_requires_identity_and_exact_secret_envelope():
    broker = Broker()
    app = client(broker)
    assert app.post("/v1/credentials/install", json={}).status_code == 403
    headers = {"Authorization": "Bearer valid"}
    response = app.post(
        "/v1/credentials/install",
        headers=headers,
        json={"intent_id": str(INTENT), "credential": {"refresh_token": "secret"}},
    )
    assert response.status_code == 204
    assert broker.calls == [
        ("install", INTENT, {"refresh_token": "secret"})
    ]
    assert app.post(
        "/v1/credentials/install",
        headers=headers,
        json={"intent_id": str(INTENT), "credential": {}, "tenant_id": str(INTENT)},
    ).status_code == 400


def test_health_is_content_free_and_broker_errors_are_generic():
    app = client(FailingBroker())
    assert app.get("/healthz").get_json() == {"status": "ok"}
    response = app.post(
        "/v1/credentials/install",
        headers={"Authorization": "Bearer valid"},
        json={"intent_id": str(INTENT), "credential": {"token": "secret"}},
    )
    assert response.status_code == 503
    assert b"secret detail" not in response.data


def test_revoke_accepts_no_credential_or_tenant_fields():
    broker = Broker()
    app = client(broker)
    headers = {"Authorization": "Bearer valid"}
    assert app.post(
        "/v1/credentials/revoke",
        headers=headers,
        json={"intent_id": str(INTENT)},
    ).status_code == 204
    assert app.post(
        "/v1/credentials/revoke",
        headers=headers,
        json={"intent_id": str(INTENT), "credential": {"token": "secret"}},
    ).status_code == 400
