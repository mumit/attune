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
WORKER = "worker@example.iam.gserviceaccount.com"
OAUTH_EXCHANGE = "oauth-exchange@example.iam.gserviceaccount.com"


class Broker:
    def __init__(self):
        self.calls = []

    def install(self, intent_id, credential):
        self.calls.append(("install", intent_id, credential))
        return SimpleNamespace(status_code=204)

    def revoke(self, intent_id):
        self.calls.append(("revoke", intent_id))
        return SimpleNamespace(status_code=204)

    def google_gmail_profile(self, intent_id):
        self.calls.append(("google_gmail_profile", intent_id))
        return SimpleNamespace(
            status_code=200,
            body={"history_id": "123", "messages_total": 4, "threads_total": 3},
        )

    def google_calendar_primary(self, intent_id):
        self.calls.append(("google_calendar_primary", intent_id))
        return SimpleNamespace(status_code=204, body=None)

    def google_oauth_exchange(self, intent_id, **kwargs):
        self.calls.append(("google_oauth_exchange", intent_id, kwargs))
        return SimpleNamespace(status_code=204)


class FailingBroker(Broker):
    def install(self, intent_id, credential):
        raise RuntimeError("secret detail")


class DeniedUseBroker(Broker):
    def google_gmail_profile(self, intent_id):
        return SimpleNamespace(status_code=404, body=None)


def claims(token, audience):
    assert token in {"valid", "valid-worker", "valid-oauth"} and audience == AUDIENCE
    now = int(time.time())
    return {
        "iss": "https://accounts.google.com",
        "aud": AUDIENCE,
        "email": {
            "valid": CALLER,
            "valid-worker": WORKER,
            "valid-oauth": OAUTH_EXCHANGE,
        }[token],
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
        expected_worker=WORKER,
        expected_oauth_exchange=OAUTH_EXCHANGE,
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
    assert broker.calls == [("install", INTENT, {"refresh_token": "secret"})]
    assert (
        app.post(
            "/v1/credentials/install",
            headers=headers,
            json={"intent_id": str(INTENT), "credential": {}, "tenant_id": str(INTENT)},
        ).status_code
        == 400
    )


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
    assert (
        app.post(
            "/v1/credentials/revoke",
            headers=headers,
            json={"intent_id": str(INTENT)},
        ).status_code
        == 204
    )
    assert (
        app.post(
            "/v1/credentials/revoke",
            headers=headers,
            json={"intent_id": str(INTENT), "credential": {"token": "secret"}},
        ).status_code
        == 400
    )


def test_google_profile_requires_worker_and_returns_only_bounded_result():
    broker = Broker()
    app = client(broker)
    body = {"intent_id": str(INTENT)}
    route = "/v1/providers/google/gmail/profile"
    assert (
        app.post(route, headers={"Authorization": "Bearer valid"}, json=body).status_code
        == 403
    )
    response = app.post(
        route, headers={"Authorization": "Bearer valid-worker"}, json=body
    )
    assert response.status_code == 200
    assert response.get_json() == {
        "history_id": "123",
        "messages_total": 4,
        "threads_total": 3,
    }
    assert broker.calls == [("google_gmail_profile", INTENT)]


def test_google_calendar_requires_worker_and_returns_no_provider_data():
    broker = Broker()
    app = client(broker)
    route = "/v1/providers/google/calendar/primary"
    body = {"intent_id": str(INTENT)}
    assert (
        app.post(route, headers={"Authorization": "Bearer valid"}, json=body).status_code
        == 403
    )
    response = app.post(
        route, headers={"Authorization": "Bearer valid-worker"}, json=body
    )
    assert response.status_code == 204 and response.data == b""
    assert broker.calls == [("google_calendar_primary", INTENT)]


def test_worker_cannot_install_or_add_provider_arguments():
    app = client(Broker())
    worker = {"Authorization": "Bearer valid-worker"}
    assert (
        app.post(
            "/v1/credentials/install",
            headers=worker,
            json={"intent_id": str(INTENT), "credential": {"token": "secret"}},
        ).status_code
        == 403
    )
    assert (
        app.post(
            "/v1/providers/google/gmail/profile",
            headers=worker,
            json={"intent_id": str(INTENT), "user_id": "victim@example.com"},
        ).status_code
        == 400
    )


def test_use_anomaly_log_is_fixed_and_content_free(caplog):
    response = client(DeniedUseBroker()).post(
        "/v1/providers/google/gmail/profile",
        headers={"Authorization": "Bearer valid-worker"},
        json={"intent_id": str(INTENT)},
    )
    assert response.status_code == 404
    assert "attune_secret_broker_use_anomaly status=404" in caplog.text
    assert str(INTENT) not in caplog.text


def test_google_oauth_exchange_requires_dedicated_identity_and_exact_contract():
    broker = Broker()
    app = client(broker)
    route = "/v1/oauth/google/exchange"
    body = {
        "intent_id": str(INTENT),
        "code": "code",
        "pkce_verifier": "v" * 64,
        "nonce_hash": "a" * 64,
        "redirect_uri": "https://dev.attune.mumit.org/oauth/google/callback",
        "scopes": ["openid", "email"],
    }
    assert (
        app.post(route, headers={"Authorization": "Bearer valid"}, json=body).status_code
        == 403
    )
    assert (
        app.post(
            route, headers={"Authorization": "Bearer valid-oauth"}, json=body
        ).status_code
        == 204
    )
    assert broker.calls[-1][0:2] == ("google_oauth_exchange", INTENT)
    assert "tenant_id" not in broker.calls[-1][2]
    assert (
        app.post(
            route,
            headers={"Authorization": "Bearer valid-oauth"},
            json={**body, "provider_url": "https://attacker.example"},
        ).status_code
        == 400
    )
