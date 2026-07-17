from __future__ import annotations

import time
from types import SimpleNamespace
from uuid import UUID

import pytest

from attune.hosted.dispatch_broker_service import create_app

INTENT = UUID("a0000000-0000-4000-8000-000000000621")
AUDIENCE = "https://dispatch-broker.attune.internal"
CALLERS = {
    "control_plane": "control@example.iam.gserviceaccount.com",
    "ingress": "ingress@example.iam.gserviceaccount.com",
    "worker": "worker@example.iam.gserviceaccount.com",
}
SLACK_INGRESS_EMAIL = "slack-ingress@example.iam.gserviceaccount.com"
MULTI_INGRESS_CALLERS = {
    **CALLERS,
    "ingress": (CALLERS["ingress"], SLACK_INGRESS_EMAIL),
}


class Broker:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def dispatch(self, intent_id, *, producer_kind):
        self.calls.append((intent_id, producer_kind))
        if self.error:
            raise self.error
        return SimpleNamespace(status_code=204)


def claims(token, audience):
    email = {
        "control": CALLERS["control_plane"],
        "ingress": CALLERS["ingress"],
        "worker": CALLERS["worker"],
        "unknown": "unknown@example.iam.gserviceaccount.com",
        "slack_ingress": SLACK_INGRESS_EMAIL,
    }[token]
    now = int(time.time())
    return {
        "iss": "https://accounts.google.com",
        "aud": audience,
        "email": email,
        "email_verified": True,
        "sub": token,
        "iat": now - 10,
        "exp": now + 300,
    }


def client(broker, callers=CALLERS):
    return create_app(
        broker,
        expected_audience=AUDIENCE,
        expected_callers=callers,
        token_verifier=claims,
    ).test_client()


def test_broker_derives_producer_from_oidc_not_body():
    broker = Broker()
    app = client(broker)
    for token, kind in (
        ("control", "control_plane"),
        ("ingress", "ingress"),
        ("worker", "worker"),
    ):
        response = app.post(
            "/v1/dispatch-intents/dispatch",
            headers={"Authorization": f"Bearer {token}"},
            json={"intent_id": str(INTENT)},
        )
        assert response.status_code == 204
        assert broker.calls[-1] == (INTENT, kind)
    assert app.post(
        "/v1/dispatch-intents/dispatch",
        headers={"Authorization": "Bearer unknown"},
        json={"intent_id": str(INTENT)},
    ).status_code == 403


def test_broker_accepts_only_exact_canonical_intent_envelope():
    broker = Broker()
    app = client(broker)
    headers = {"Authorization": "Bearer control"}
    for body in (
        {},
        {"intent_id": str(INTENT).upper()},
        {"intent_id": str(INTENT), "producer_kind": "worker"},
        {"intent_id": "invalid"},
    ):
        assert app.post(
            "/v1/dispatch-intents/dispatch", headers=headers, json=body
        ).status_code == 400
    assert broker.calls == []


def test_health_is_content_free_and_failures_are_generic():
    app = client(Broker(RuntimeError("sensitive detail")))
    assert app.get("/healthz").get_json() == {"status": "ok"}
    response = app.post(
        "/v1/dispatch-intents/dispatch",
        headers={"Authorization": "Bearer worker"},
        json={"intent_id": str(INTENT)},
    )
    assert response.status_code == 503
    assert b"sensitive detail" not in response.data


def test_second_ingress_email_is_authorized_as_ingress_producer():
    broker = Broker()
    app = client(broker, callers=MULTI_INGRESS_CALLERS)
    response = app.post(
        "/v1/dispatch-intents/dispatch",
        headers={"Authorization": "Bearer slack_ingress"},
        json={"intent_id": str(INTENT)},
    )
    assert response.status_code == 204
    assert broker.calls[-1] == (INTENT, "ingress")
    # the original ingress identity remains authorized alongside the new one
    response = app.post(
        "/v1/dispatch-intents/dispatch",
        headers={"Authorization": "Bearer ingress"},
        json={"intent_id": str(INTENT)},
    )
    assert response.status_code == 204
    assert broker.calls[-1] == (INTENT, "ingress")


def test_unmapped_email_is_refused_even_with_a_valid_token():
    broker = Broker()
    app = client(broker, callers=MULTI_INGRESS_CALLERS)
    response = app.post(
        "/v1/dispatch-intents/dispatch",
        headers={"Authorization": "Bearer unknown"},
        json={"intent_id": str(INTENT)},
    )
    assert response.status_code == 403
    assert broker.calls == []


def test_duplicate_emails_across_kinds_are_rejected_at_construction():
    duplicated_callers = {
        **CALLERS,
        "worker": CALLERS["ingress"],
    }
    with pytest.raises(ValueError):
        create_app(
            Broker(),
            expected_audience=AUDIENCE,
            expected_callers=duplicated_callers,
            token_verifier=claims,
        )
