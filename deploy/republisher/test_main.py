"""Tests for the republisher (Calendar webhook + Chat interaction routes).

No live Flask server, no live GCP — Flask's test client plus injected fake
publishers and a fake JWT verifier (mirroring the attune package's own
convention of injecting every collaborator so nothing here needs live
credentials to test).

Run with: pip install -r requirements.txt pytest && pytest test_main.py
(Not part of the main `pytest` run at the repo root — this service has its
own dependency set, independent of the attune package.)
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
# Finding F4's negative matrix: malformed token, wrong audience, expired
# token — each exercised through the same injected verify_fn seam real
# ``google.auth``/``id_token.verify_oauth2_token`` sits behind in
# production, since that's the only place these failure modes are
# observable without a live Google-signed token. Combined with the tests
# above (missing token, wrong email claim, generic invalid token) and below
# (correct token accepted), this is the complete matrix Finding F4 asked
# for; the one thing none of it proves is a live Chat app's own tokens
# round-tripping — see main.py's module docstring.
# ---------------------------------------------------------------------------


def test_verify_chat_request_rejects_malformed_token():
    """A token that isn't even JWT-shaped (no three dot-separated segments)
    — google-auth's real verifier raises a DecodeError-style exception for
    this; here the fake verifier raises the same shape of error so the
    rejection path is exercised without needing a live JWT library call."""
    verify_fn = _verify_fn(
        raise_exc=ValueError("Wrong number of segments in token: b'not-a-jwt'")
    )
    ok = verify_chat_request(
        {"Authorization": "Bearer not-a-jwt"}, audience="aud", verify_fn=verify_fn
    )
    assert ok is False


def test_verify_chat_request_rejects_wrong_audience():
    """google-auth's verify_oauth2_token itself raises when the token's
    ``aud`` claim doesn't match the audience it was asked to verify against
    — simulate that real behavior in the injected verifier rather than
    relying on verify_chat_request to do its own audience comparison (it
    doesn't; the audience check is the verifier's job, which is exactly why
    this case matters as its own test)."""

    def _fn(token, audience):
        if audience != "https://correct-service.example/chat-interaction":
            raise ValueError(
                f"Wrong recipient, payload audience != requested audience, "
                f"got {audience!r}"
            )
        return {"email": "chat@system.gserviceaccount.com"}

    ok = verify_chat_request(
        {"Authorization": "Bearer good-token"},
        audience="https://attacker-controlled.example/chat-interaction",
        verify_fn=_fn,
    )
    assert ok is False


def test_verify_chat_request_rejects_expired_token():
    """However google-auth surfaces expiry (its exact exception type isn't
    this module's concern — verify_chat_request's ``except Exception``
    catches whatever shape it takes), an expired token must not verify."""
    verify_fn = _verify_fn(
        raise_exc=ValueError("Token expired, 1700000000 < 1700003600")
    )
    ok = verify_chat_request(
        {"Authorization": "Bearer expired-token"}, audience="aud", verify_fn=verify_fn
    )
    assert ok is False


def test_chat_interaction_route_rejects_wrong_audience(client):
    """Route-level companion to test_verify_chat_request_rejects_wrong_audience
    — confirms the 403 actually surfaces through /chat-interaction, not just
    through the unit-level helper."""
    fake = _FakePublisher()
    app.config["INTERACTION_PUBLISHER"] = fake
    app.config["INTERACTION_TOPIC"] = "projects/p/topics/chat-interaction"
    app.config["CHAT_AUDIENCE"] = "https://correct-service.example/chat-interaction"

    def _fn(token, audience):
        if audience != "https://correct-service.example/chat-interaction":
            raise ValueError("Wrong recipient")
        return {"email": "chat@system.gserviceaccount.com"}

    app.config["VERIFY_CHAT_FN"] = _fn
    app.config["CHAT_AUDIENCE"] = "https://wrong-service.example/chat-interaction"

    resp = client.post(
        "/chat-interaction",
        json=_chat_click("attune_approve", "t-1"),
        headers=_authed(),
    )

    assert resp.status_code == 403
    assert fake.calls == []


def test_chat_interaction_route_rejects_expired_token(client):
    fake = _FakePublisher()
    app.config["INTERACTION_PUBLISHER"] = fake
    app.config["INTERACTION_TOPIC"] = "projects/p/topics/chat-interaction"
    app.config["VERIFY_CHAT_FN"] = _verify_fn(
        raise_exc=ValueError("Token expired")
    )

    resp = client.post(
        "/chat-interaction",
        json=_chat_click("attune_approve", "t-1"),
        headers=_authed(),
    )

    assert resp.status_code == 403
    assert fake.calls == []


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
        json=_chat_click("attune_edit"),
        headers=_authed(),
    )

    assert resp.status_code == 200
    assert resp.get_json()["actionResponse"]["type"] == "DIALOG"
    assert fake.calls == []


def test_chat_message_is_verified_and_republished(client):
    fake = _FakePublisher()
    app.config["INTERACTION_PUBLISHER"] = fake
    app.config["INTERACTION_TOPIC"] = "projects/p/topics/chat-interaction"
    app.config["VERIFY_CHAT_FN"] = _verify_fn(claims={"email": "chat@system.gserviceaccount.com"})

    event = {
        "type": "MESSAGE",
        "message": {
            "text": "brief",
            "sender": {"name": "users/U1", "type": "HUMAN"},
            "space": {"name": "spaces/S1"},
        },
    }
    resp = client.post("/chat-interaction", json=event, headers=_authed())

    assert resp.status_code == 200
    assert json.loads(fake.calls[0][1]) == event


def test_chat_interaction_edit_dialog_prefilled_from_echoed_card(client):
    """The dialog prefills the draft from the card echoed in the event —
    this service is stateless, so the event is the only source (prompt 02)."""
    fake = _FakePublisher()
    app.config["INTERACTION_PUBLISHER"] = fake
    app.config["INTERACTION_TOPIC"] = "projects/p/topics/chat-interaction"
    app.config["VERIFY_CHAT_FN"] = _verify_fn(claims={"email": "chat@system.gserviceaccount.com"})

    event = _chat_click("attune_edit", "t-7")
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
    assert widgets[0]["textInput"]["name"] == "attune_edit_text"
    assert widgets[0]["textInput"]["value"] == "Original draft."
    submit_action = widgets[1]["buttonList"]["buttons"][0]["onClick"]["action"]
    assert submit_action["function"] == "attune_edit_submit"
    assert submit_action["parameters"] == [{"key": "thread_id", "value": "t-7"}]
    assert fake.calls == []


def test_chat_interaction_edit_submit_publishes_and_closes_dialog(client):
    """The dialog's submit is a real graph resume, so it rides Pub/Sub like
    approve/reject; the sync response just closes the dialog."""
    fake = _FakePublisher()
    app.config["INTERACTION_PUBLISHER"] = fake
    app.config["INTERACTION_TOPIC"] = "projects/p/topics/chat-interaction"
    app.config["VERIFY_CHAT_FN"] = _verify_fn(claims={"email": "chat@system.gserviceaccount.com"})

    event = _chat_click("attune_edit_submit", "t-7")
    event["common"] = {
        "formInputs": {"attune_edit_text": {"stringInputs": {"value": ["My rewrite."]}}}
    }
    resp = client.post("/chat-interaction", json=event, headers=_authed())

    assert resp.status_code == 200
    action_response = resp.get_json()["actionResponse"]
    assert action_response["type"] == "DIALOG"
    assert action_response["dialogAction"]["actionStatus"]["statusCode"] == "OK"
    assert len(fake.calls) == 1
    _, data = fake.calls[0]
    assert json.loads(data)["action"]["actionMethodName"] == "attune_edit_submit"


def test_chat_interaction_approve_publishes_and_acks(client):
    fake = _FakePublisher()
    app.config["INTERACTION_PUBLISHER"] = fake
    app.config["INTERACTION_TOPIC"] = "projects/p/topics/chat-interaction"
    app.config["VERIFY_CHAT_FN"] = _verify_fn(claims={"email": "chat@system.gserviceaccount.com"})

    resp = client.post(
        "/chat-interaction",
        json=_chat_click("attune_approve", "t-42"),
        headers=_authed(),
    )

    assert resp.status_code == 200
    assert "Processing" in resp.get_json()["text"]
    assert len(fake.calls) == 1
    topic, data = fake.calls[0]
    assert topic == "projects/p/topics/chat-interaction"
    payload = json.loads(data)
    assert payload["action"]["actionMethodName"] == "attune_approve"


def test_chat_interaction_reject_publishes_and_acks(client):
    fake = _FakePublisher()
    app.config["INTERACTION_PUBLISHER"] = fake
    app.config["INTERACTION_TOPIC"] = "projects/p/topics/chat-interaction"
    app.config["VERIFY_CHAT_FN"] = _verify_fn(claims={"email": "chat@system.gserviceaccount.com"})

    resp = client.post(
        "/chat-interaction",
        json=_chat_click("attune_reject", "t-9"),
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
        json=_chat_click("attune_approve", "t-42"),
        headers=_authed(),
    )

    assert resp.status_code == 403
    assert fake.calls == []


def test_chat_interaction_missing_auth_header_returns_403(client):
    fake = _FakePublisher()
    app.config["INTERACTION_PUBLISHER"] = fake
    app.config["INTERACTION_TOPIC"] = "projects/p/topics/chat-interaction"
    app.config["VERIFY_CHAT_FN"] = _verify_fn(claims={"email": "chat@system.gserviceaccount.com"})

    resp = client.post("/chat-interaction", json=_chat_click("attune_approve", "t-42"))

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
