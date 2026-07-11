"""Tests for the republisher (Calendar webhook + Chat interaction routes).

No live Flask server, no live GCP — Flask's test client plus injected fake
publishers and a fake JWT verifier (mirroring the aidedecamp package's own
convention of injecting every collaborator so nothing here needs live
credentials to test).

Run with: pip install -r requirements.txt pytest && pytest test_main.py
(Not part of the main `pytest` run at the repo root — this service has its
own dependency set, independent of the aidedecamp package.)
"""

from __future__ import annotations

import json

import pytest

from main import app, decode_headers, publish, verify_chat_request


class _FakeFuture:
    def __init__(self, raise_exc: Exception | None = None):
        self._raise_exc = raise_exc

    def result(self, timeout=None):
        if self._raise_exc:
            raise self._raise_exc
        return "message-id-123"


class _FakePublisher:
    def __init__(self, raise_exc: Exception | None = None):
        self.calls: list[tuple] = []
        self._raise_exc = raise_exc

    def publish(self, topic, data):
        self.calls.append((topic, data))
        return _FakeFuture(self._raise_exc)


@pytest.fixture()
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# decode_headers
# ---------------------------------------------------------------------------


def test_decode_headers_extracts_all_fields():
    headers = {
        "X-Goog-Channel-ID": "chan-1",
        "X-Goog-Resource-ID": "res-1",
        "X-Goog-Resource-State": "exists",
        "X-Goog-Message-Number": "42",
        "Content-Type": "application/json",
    }
    decoded = decode_headers(headers)
    assert decoded == {
        "channel_id": "chan-1",
        "resource_id": "res-1",
        "resource_state": "exists",
        "message_number": "42",
    }


def test_decode_headers_missing_fields_default_empty():
    assert decode_headers({}) == {
        "channel_id": "",
        "resource_id": "",
        "resource_state": "",
        "message_number": "",
    }


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


def test_publish_sends_json_payload():
    fake = _FakePublisher()
    publish(fake, "projects/p/topics/t", {"channel_id": "c1"})

    assert len(fake.calls) == 1
    topic, data = fake.calls[0]
    assert topic == "projects/p/topics/t"
    assert json.loads(data) == {"channel_id": "c1"}


def test_publish_waits_for_result_and_propagates_failure():
    fake = _FakePublisher(raise_exc=RuntimeError("pubsub down"))
    with pytest.raises(RuntimeError, match="pubsub down"):
        publish(fake, "projects/p/topics/t", {"channel_id": "c1"})


# ---------------------------------------------------------------------------
# /calendar-webhook endpoint
# ---------------------------------------------------------------------------


def test_webhook_returns_200(client):
    fake = _FakePublisher()
    app.config["PUBLISHER"] = fake
    app.config["TOPIC"] = "projects/p/topics/calendar"

    resp = client.post(
        "/calendar-webhook",
        headers={
            "X-Goog-Channel-ID": "chan-1",
            "X-Goog-Resource-ID": "res-1",
            "X-Goog-Resource-State": "exists",
            "X-Goog-Message-Number": "1",
        },
    )

    assert resp.status_code == 200


def test_webhook_publishes_decoded_headers(client):
    fake = _FakePublisher()
    app.config["PUBLISHER"] = fake
    app.config["TOPIC"] = "projects/p/topics/calendar"

    client.post(
        "/calendar-webhook",
        headers={
            "X-Goog-Channel-ID": "chan-1",
            "X-Goog-Resource-ID": "res-1",
            "X-Goog-Resource-State": "sync",
            "X-Goog-Message-Number": "1",
        },
    )

    assert len(fake.calls) == 1
    topic, data = fake.calls[0]
    assert topic == "projects/p/topics/calendar"
    payload = json.loads(data)
    assert payload["channel_id"] == "chan-1"
    assert payload["resource_state"] == "sync"


def test_webhook_uses_configured_topic(client):
    fake = _FakePublisher()
    app.config["PUBLISHER"] = fake
    app.config["TOPIC"] = "projects/other/topics/other-calendar"

    client.post("/calendar-webhook", headers={"X-Goog-Channel-ID": "c1"})

    topic, _ = fake.calls[0]
    assert topic == "projects/other/topics/other-calendar"


def test_webhook_get_not_allowed(client):
    resp = client.get("/calendar-webhook")
    assert resp.status_code == 405


# ---------------------------------------------------------------------------
# verify_chat_request
# ---------------------------------------------------------------------------


def _verify_fn(claims=None, raise_exc=None):
    def _fn(token, audience):
        if raise_exc:
            raise raise_exc
        return claims

    return _fn


def test_verify_chat_request_accepts_valid_chat_caller_email():
    verify_fn = _verify_fn(claims={"email": "chat@system.gserviceaccount.com"})
    ok = verify_chat_request(
        {"Authorization": "Bearer good-token"}, audience="aud", verify_fn=verify_fn
    )
    assert ok is True


def test_verify_chat_request_rejects_wrong_caller_email():
    verify_fn = _verify_fn(claims={"email": "someone-else@example.com"})
    ok = verify_chat_request(
        {"Authorization": "Bearer good-token"}, audience="aud", verify_fn=verify_fn
    )
    assert ok is False


def test_verify_chat_request_rejects_missing_email_claim():
    """A token that verifies fine but carries no email claim at all (e.g. the
    wrong claim shape) must not be treated as Chat — only an exact email
    match authenticates the caller."""
    verify_fn = _verify_fn(claims={"iss": "https://accounts.google.com"})
    ok = verify_chat_request(
        {"Authorization": "Bearer good-token"}, audience="aud", verify_fn=verify_fn
    )
    assert ok is False


def test_verify_chat_request_rejects_missing_auth_header():
    verify_fn = _verify_fn(claims={"email": "chat@system.gserviceaccount.com"})
    ok = verify_chat_request({}, audience="aud", verify_fn=verify_fn)
    assert ok is False


def test_verify_chat_request_rejects_non_bearer_auth_header():
    verify_fn = _verify_fn(claims={"email": "chat@system.gserviceaccount.com"})
    ok = verify_chat_request(
        {"Authorization": "Basic xyz"}, audience="aud", verify_fn=verify_fn
    )
    assert ok is False


def test_verify_chat_request_rejects_invalid_token():
    verify_fn = _verify_fn(raise_exc=ValueError("invalid token"))
    ok = verify_chat_request(
        {"Authorization": "Bearer bad-token"}, audience="aud", verify_fn=verify_fn
    )
    assert ok is False


# ---------------------------------------------------------------------------
# /chat-interaction endpoint
# ---------------------------------------------------------------------------


def _chat_click(fn: str, thread_id: str = "t-1") -> dict:
    return {
        "type": "CARD_CLICKED",
        "action": {
            "actionMethodName": fn,
            "parameters": [{"key": "thread_id", "value": thread_id}],
        },
    }


def _authed(client_kwargs=None):
    return {"Authorization": "Bearer good-token", **(client_kwargs or {})}


def test_chat_interaction_edit_returns_dialog_without_publishing(client):
    fake = _FakePublisher()
    app.config["INTERACTION_PUBLISHER"] = fake
    app.config["INTERACTION_TOPIC"] = "projects/p/topics/chat-interaction"
    app.config["VERIFY_CHAT_FN"] = _verify_fn(claims={"email": "chat@system.gserviceaccount.com"})

    resp = client.post(
        "/chat-interaction",
        json=_chat_click("adc_edit"),
        headers=_authed(),
    )

    assert resp.status_code == 200
    assert resp.get_json()["actionResponse"]["type"] == "DIALOG"
    assert fake.calls == []


def test_chat_interaction_edit_dialog_prefilled_from_echoed_card(client):
    """The dialog prefills the draft from the card echoed in the event —
    this service is stateless, so the event is the only source (prompt 02)."""
    fake = _FakePublisher()
    app.config["INTERACTION_PUBLISHER"] = fake
    app.config["INTERACTION_TOPIC"] = "projects/p/topics/chat-interaction"
    app.config["VERIFY_CHAT_FN"] = _verify_fn(claims={"email": "chat@system.gserviceaccount.com"})

    event = _chat_click("adc_edit", "t-7")
    event["message"] = {
        "cardsV2": [
            {
                "card": {
                    "sections": [
                        {"widgets": [{"textParagraph": {"text": "Original draft."}}]}
                    ]
                }
            }
        ]
    }
    resp = client.post("/chat-interaction", json=event, headers=_authed())

    dialog = resp.get_json()["actionResponse"]["dialogAction"]["dialog"]
    widgets = dialog["body"]["sections"][0]["widgets"]
    assert widgets[0]["textInput"]["name"] == "adc_edit_text"
    assert widgets[0]["textInput"]["value"] == "Original draft."
    submit_action = widgets[1]["buttonList"]["buttons"][0]["onClick"]["action"]
    assert submit_action["function"] == "adc_edit_submit"
    assert submit_action["parameters"] == [{"key": "thread_id", "value": "t-7"}]
    assert fake.calls == []


def test_chat_interaction_edit_submit_publishes_and_closes_dialog(client):
    """The dialog's submit is a real graph resume, so it rides Pub/Sub like
    approve/reject; the sync response just closes the dialog."""
    fake = _FakePublisher()
    app.config["INTERACTION_PUBLISHER"] = fake
    app.config["INTERACTION_TOPIC"] = "projects/p/topics/chat-interaction"
    app.config["VERIFY_CHAT_FN"] = _verify_fn(claims={"email": "chat@system.gserviceaccount.com"})

    event = _chat_click("adc_edit_submit", "t-7")
    event["common"] = {
        "formInputs": {"adc_edit_text": {"stringInputs": {"value": ["My rewrite."]}}}
    }
    resp = client.post("/chat-interaction", json=event, headers=_authed())

    assert resp.status_code == 200
    action_response = resp.get_json()["actionResponse"]
    assert action_response["type"] == "DIALOG"
    assert action_response["dialogAction"]["actionStatus"]["statusCode"] == "OK"
    assert len(fake.calls) == 1
    _, data = fake.calls[0]
    assert json.loads(data)["action"]["actionMethodName"] == "adc_edit_submit"


def test_chat_interaction_approve_publishes_and_acks(client):
    fake = _FakePublisher()
    app.config["INTERACTION_PUBLISHER"] = fake
    app.config["INTERACTION_TOPIC"] = "projects/p/topics/chat-interaction"
    app.config["VERIFY_CHAT_FN"] = _verify_fn(claims={"email": "chat@system.gserviceaccount.com"})

    resp = client.post(
        "/chat-interaction",
        json=_chat_click("adc_approve", "t-42"),
        headers=_authed(),
    )

    assert resp.status_code == 200
    assert "Processing" in resp.get_json()["text"]
    assert len(fake.calls) == 1
    topic, data = fake.calls[0]
    assert topic == "projects/p/topics/chat-interaction"
    payload = json.loads(data)
    assert payload["action"]["actionMethodName"] == "adc_approve"


def test_chat_interaction_reject_publishes_and_acks(client):
    fake = _FakePublisher()
    app.config["INTERACTION_PUBLISHER"] = fake
    app.config["INTERACTION_TOPIC"] = "projects/p/topics/chat-interaction"
    app.config["VERIFY_CHAT_FN"] = _verify_fn(claims={"email": "chat@system.gserviceaccount.com"})

    resp = client.post(
        "/chat-interaction",
        json=_chat_click("adc_reject", "t-9"),
        headers=_authed(),
    )

    assert resp.status_code == 200
    assert len(fake.calls) == 1


def test_chat_interaction_unauthenticated_returns_403_without_publishing(client):
    fake = _FakePublisher()
    app.config["INTERACTION_PUBLISHER"] = fake
    app.config["INTERACTION_TOPIC"] = "projects/p/topics/chat-interaction"
    app.config["VERIFY_CHAT_FN"] = _verify_fn(raise_exc=ValueError("bad token"))

    resp = client.post(
        "/chat-interaction",
        json=_chat_click("adc_approve", "t-42"),
        headers=_authed(),
    )

    assert resp.status_code == 403
    assert fake.calls == []


def test_chat_interaction_missing_auth_header_returns_403(client):
    fake = _FakePublisher()
    app.config["INTERACTION_PUBLISHER"] = fake
    app.config["INTERACTION_TOPIC"] = "projects/p/topics/chat-interaction"
    app.config["VERIFY_CHAT_FN"] = _verify_fn(claims={"email": "chat@system.gserviceaccount.com"})

    resp = client.post("/chat-interaction", json=_chat_click("adc_approve", "t-42"))

    assert resp.status_code == 403
    assert fake.calls == []


def test_chat_interaction_unknown_action_returns_200_without_publishing(client):
    fake = _FakePublisher()
    app.config["INTERACTION_PUBLISHER"] = fake
    app.config["INTERACTION_TOPIC"] = "projects/p/topics/chat-interaction"
    app.config["VERIFY_CHAT_FN"] = _verify_fn(claims={"email": "chat@system.gserviceaccount.com"})

    resp = client.post(
        "/chat-interaction",
        json=_chat_click("unknown_fn", "t-1"),
        headers=_authed(),
    )

    assert resp.status_code == 200
    assert fake.calls == []
