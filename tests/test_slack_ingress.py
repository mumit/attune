import hashlib
import hmac

import pytest

from attune.hosted.slack_ingress import (
    decode_owner_dm_message_diagnostic,
    decode_url_verification,
    verify_slack_signature,
)

SECRET = b"8f742231b10e8888abcd99yyyzzz85a5"
NOW = 1_752_600_000


def sign(body: bytes, timestamp: int, secret: bytes = SECRET) -> str:
    basestring = b"v0:" + str(timestamp).encode() + b":" + body
    return "v0=" + hmac.new(secret, basestring, hashlib.sha256).hexdigest()


def event(**overrides):
    message = {
        "type": "message",
        "channel_type": "im",
        "user": "U0123456789",
        "channel": "D0123456789",
        "ts": "1752600000.000100",
        "text": "what is on my calendar tomorrow?",
        "team": "T0123456789",
    }
    payload = {
        "type": "event_callback",
        "team_id": "T0123456789",
        "event": message,
    }
    for key, value in overrides.items():
        if value is None:
            message.pop(key, None)
        else:
            message[key] = value
    return payload


def test_signature_verifies_only_exact_hmac_within_window():
    body = b'{"type":"event_callback"}'
    assert verify_slack_signature(
        signing_secret=SECRET,
        timestamp_header=str(NOW),
        signature_header=sign(body, NOW),
        raw_body=body,
        now=NOW + 100,
    )
    assert not verify_slack_signature(
        signing_secret=SECRET,
        timestamp_header=str(NOW),
        signature_header=sign(body, NOW),
        raw_body=body + b" ",
        now=NOW + 100,
    )
    assert not verify_slack_signature(
        signing_secret=SECRET,
        timestamp_header=str(NOW),
        signature_header=sign(body, NOW, b"another-signing-secret-value-abc"),
        raw_body=body,
        now=NOW + 100,
    )


def test_signature_rejects_stale_or_future_timestamps_and_bad_headers():
    body = b"{}"
    for skew in (301, -301):
        assert not verify_slack_signature(
            signing_secret=SECRET,
            timestamp_header=str(NOW),
            signature_header=sign(body, NOW),
            raw_body=body,
            now=NOW + skew,
        )
    for timestamp, signature in (
        (None, sign(body, NOW)),
        ("not-a-number", sign(body, NOW)),
        (str(NOW), None),
        (str(NOW), "v0=zz"),
        (str(NOW), sign(body, NOW).replace("v0=", "v1=")),
    ):
        assert not verify_slack_signature(
            signing_secret=SECRET,
            timestamp_header=timestamp,
            signature_header=signature,
            raw_body=body,
            now=NOW,
        )


def test_signing_secret_shape_is_validated():
    with pytest.raises(ValueError):
        verify_slack_signature(
            signing_secret=b"short",
            timestamp_header=str(NOW),
            signature_header=sign(b"{}", NOW),
            raw_body=b"{}",
            now=NOW,
        )


def test_url_verification_decodes_only_exact_handshake():
    assert decode_url_verification(
        {"type": "url_verification", "challenge": "abc"}
    ).challenge == "abc"
    assert decode_url_verification({"type": "event_callback"}) is None
    assert decode_url_verification({"type": "url_verification", "challenge": 5}) is None


def test_owner_dm_message_is_normalized_with_domain_prefixed_references():
    message, rejection = decode_owner_dm_message_diagnostic(event())
    assert rejection == "accepted"
    assert message.text == "what is on my calendar tomorrow?"
    assert message.team_ref == "teams/T0123456789"
    assert message.actor_ref == "teams/T0123456789/users/U0123456789"
    assert message.destination_ref == "teams/T0123456789/channels/D0123456789"
    assert message.message_ref == (
        "teams/T0123456789/channels/D0123456789/messages/1752600000.000100"
    )
    assert "calendar" not in repr(message)


def test_owner_dm_message_accepts_events_without_nested_team():
    message, rejection = decode_owner_dm_message_diagnostic(event(team=None))
    assert rejection == "accepted"
    assert message.team_ref == "teams/T0123456789"


@pytest.mark.parametrize(
    "overrides,rejection",
    [
        ({"channel_type": "channel"}, "event_shape"),
        ({"subtype": "message_changed"}, "event_shape"),
        ({"bot_id": "B0123456789"}, "event_shape"),
        ({"bot_profile": {"id": "B0123456789"}}, "event_shape"),
        ({"edited": {"ts": "1752600001.000000"}}, "event_shape"),
        ({"team": "T9999999999"}, "actor_channel_binding"),
        ({"user": "not-a-user"}, "actor_channel_binding"),
        ({"channel": "C0123456789"}, "actor_channel_binding"),
        ({"ts": "not-a-ts"}, "actor_channel_binding"),
        ({"text": ""}, "message_body"),
        ({"text": "x" * 8_001}, "message_body"),
    ],
)
def test_owner_dm_message_rejects_noncanonical_events(overrides, rejection):
    message, reason = decode_owner_dm_message_diagnostic(event(**overrides))
    assert message is None
    assert reason == rejection


def test_owner_dm_message_rejects_foreign_envelopes():
    assert decode_owner_dm_message_diagnostic({"type": "block_actions"})[1] == "event_envelope"
    assert decode_owner_dm_message_diagnostic(None)[1] == "event_envelope"
    envelope = event()
    envelope["team_id"] = "not-a-team"
    assert decode_owner_dm_message_diagnostic(envelope)[1] == "event_envelope"
