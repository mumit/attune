import hashlib
import hmac
import json
from uuid import UUID

from attune.hosted.slack_ingress_service import create_app

SECRET = b"8f742231b10e8888abcd99yyyzzz85a5"
NOW = 1_752_600_000
INTENT = UUID("10000000-0000-4000-8000-000000000111")


class Broker:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def accept_slack_message(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return INTENT


class Dispatcher:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def dispatch(self, intent_id):
        self.calls.append(intent_id)
        return self.result


def signed(body: dict, *, timestamp: int = NOW, secret: bytes = SECRET):
    raw = json.dumps(body).encode()
    basestring = b"v0:" + str(timestamp).encode() + b":" + raw
    signature = "v0=" + hmac.new(secret, basestring, hashlib.sha256).hexdigest()
    return raw, {
        "X-Slack-Request-Timestamp": str(timestamp),
        "X-Slack-Signature": signature,
        "Content-Type": "application/json",
    }


def dm_event():
    return {
        "type": "event_callback",
        "team_id": "T0123456789",
        "event": {
            "type": "message",
            "channel_type": "im",
            "user": "U0123456789",
            "channel": "D0123456789",
            "ts": "1752600000.000100",
            "text": "what is on my calendar tomorrow?",
        },
    }


def client(broker=None, dispatcher=None, enabled=True):
    return create_app(
        broker or Broker(),
        signing_secret=SECRET,
        dispatch_broker=dispatcher,
        conversations_enabled=enabled,
        clock=lambda: NOW,
    ).test_client()


def test_unsigned_and_missigned_requests_are_refused():
    app = client()
    raw, headers = signed(dm_event())
    assert app.post("/v1/provider/slack/events", data=raw).status_code == 403
    headers["X-Slack-Signature"] = "v0=" + "0" * 64
    assert app.post(
        "/v1/provider/slack/events", data=raw, headers=headers
    ).status_code == 403


def test_stale_timestamp_is_refused_even_with_valid_hmac():
    app = client()
    raw, headers = signed(dm_event(), timestamp=NOW - 301)
    assert app.post(
        "/v1/provider/slack/events", data=raw, headers=headers
    ).status_code == 403


def test_url_verification_handshake_returns_challenge_only_when_signed():
    app = client()
    raw, headers = signed({"type": "url_verification", "challenge": "abc"})
    response = app.post("/v1/provider/slack/events", data=raw, headers=headers)
    assert response.get_json() == {"challenge": "abc"}


def test_owner_dm_message_is_accepted_and_dispatched():
    broker, dispatcher = Broker(), Dispatcher()
    app = client(broker, dispatcher)
    raw, headers = signed(dm_event())
    response = app.post("/v1/provider/slack/events", data=raw, headers=headers)
    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
    assert broker.calls == [{
        "team_ref": "teams/T0123456789",
        "actor_ref": "teams/T0123456789/users/U0123456789",
        "destination_ref": "teams/T0123456789/channels/D0123456789",
        "message_ref": (
            "teams/T0123456789/channels/D0123456789/messages/1752600000.000100"
        ),
        "text": "what is on my calendar tomorrow?",
    }]
    assert dispatcher.calls == [INTENT]


def test_non_dm_and_bot_events_are_acknowledged_without_broker_contact():
    broker = Broker()
    app = client(broker, Dispatcher())
    body = dm_event()
    body["event"]["bot_id"] = "B0123456789"
    raw, headers = signed(body)
    assert app.post(
        "/v1/provider/slack/events", data=raw, headers=headers
    ).get_json() == {"ok": True}
    assert broker.calls == []


def test_disabled_conversations_acknowledge_without_broker_contact():
    broker = Broker()
    app = client(broker, None, enabled=False)
    raw, headers = signed(dm_event())
    assert app.post(
        "/v1/provider/slack/events", data=raw, headers=headers
    ).get_json() == {"ok": True}
    assert broker.calls == []


def test_broker_failures_are_content_free_and_still_acknowledged():
    app = client(Broker(RuntimeError("sensitive tenant value")), Dispatcher())
    raw, headers = signed(dm_event())
    response = app.post("/v1/provider/slack/events", data=raw, headers=headers)
    assert response.status_code == 200
    assert b"sensitive tenant value" not in response.data


def test_health_route_requires_no_signature():
    assert client().get("/healthz").get_json() == {"status": "ok"}
