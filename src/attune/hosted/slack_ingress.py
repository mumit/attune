"""Strict verification and normalization of Slack owner-DM events."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass

SIGNATURE_VERSION = "v0"
TIMESTAMP_WINDOW_SECONDS = 300
_TEAM_ID = re.compile(r"^T[A-Z0-9]{4,20}$")
_USER_ID = re.compile(r"^[UW][A-Z0-9]{4,20}$")
_IM_CHANNEL_ID = re.compile(r"^D[A-Z0-9]{4,20}$")
_MESSAGE_TS = re.compile(r"^[0-9]{6,20}\.[0-9]{1,10}$")
_TIMESTAMP = re.compile(r"^[0-9]{1,20}$")
_SIGNATURE = re.compile(r"^v0=[0-9a-f]{64}$")


def verify_slack_signature(
    *,
    signing_secret: bytes,
    timestamp_header: str | None,
    signature_header: str | None,
    raw_body: bytes,
    now: int,
) -> bool:
    """Constant-time v0 signature check over the unmodified request body."""
    if not isinstance(signing_secret, bytes) or not 8 <= len(signing_secret) <= 128:
        raise ValueError("Slack signing secret is invalid")
    if not isinstance(raw_body, bytes) or not isinstance(now, int):
        return False
    if (
        not isinstance(timestamp_header, str)
        or not _TIMESTAMP.fullmatch(timestamp_header)
        or not isinstance(signature_header, str)
        or not _SIGNATURE.fullmatch(signature_header)
    ):
        return False
    timestamp = int(timestamp_header)
    if abs(now - timestamp) > TIMESTAMP_WINDOW_SECONDS:
        return False
    basestring = b"v0:" + timestamp_header.encode("ascii") + b":" + raw_body
    expected = "v0=" + hmac.new(
        signing_secret, basestring, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@dataclass(frozen=True, repr=False)
class SlackUrlVerification:
    challenge: str

    def __repr__(self) -> str:
        return "SlackUrlVerification(challenge=<redacted>)"


@dataclass(frozen=True, repr=False)
class SlackOwnerDmMessage:
    text: str
    team_ref: str
    actor_ref: str
    destination_ref: str
    message_ref: str

    def __repr__(self) -> str:
        return (
            "SlackOwnerDmMessage(text=<redacted>, team_ref=<redacted>, "
            "actor_ref=<redacted>, destination_ref=<redacted>)"
        )


def decode_url_verification(payload: object) -> SlackUrlVerification | None:
    if not isinstance(payload, dict) or payload.get("type") != "url_verification":
        return None
    challenge = payload.get("challenge")
    if not isinstance(challenge, str) or not 1 <= len(challenge) <= 512:
        return None
    return SlackUrlVerification(challenge)


def decode_owner_dm_message(payload: object) -> SlackOwnerDmMessage | None:
    return decode_owner_dm_message_diagnostic(payload)[0]


def decode_owner_dm_message_diagnostic(
    payload: object,
) -> tuple[SlackOwnerDmMessage | None, str]:
    """Accept only a plain human direct message to the app.

    Every edited, deleted, threaded-broadcast, bot, app, join, or other
    subtyped event is rejected. The signed envelope's team is authoritative
    and must match the event's team when the event carries one.
    """
    if not isinstance(payload, dict) or payload.get("type") != "event_callback":
        return None, "event_envelope"
    envelope_team = payload.get("team_id")
    event = payload.get("event")
    authorizations = payload.get("authorizations")
    if not isinstance(event, dict) or not isinstance(envelope_team, str):
        return None, "event_envelope"
    if not _TEAM_ID.fullmatch(envelope_team):
        return None, "event_envelope"
    if authorizations is not None and not isinstance(authorizations, list):
        return None, "event_envelope"
    if (
        event.get("type") != "message"
        or event.get("channel_type") != "im"
        or "subtype" in event
        or "bot_id" in event
        or "bot_profile" in event
        or "edited" in event
    ):
        return None, "event_shape"
    event_team = event.get("team", envelope_team)
    user = event.get("user")
    channel = event.get("channel")
    ts = event.get("ts")
    text = event.get("text")
    if (
        event_team != envelope_team
        or not isinstance(user, str) or not _USER_ID.fullmatch(user)
        or not isinstance(channel, str) or not _IM_CHANNEL_ID.fullmatch(channel)
        or not isinstance(ts, str) or not _MESSAGE_TS.fullmatch(ts)
    ):
        return None, "actor_channel_binding"
    if not isinstance(text, str) or not 1 <= len(text) <= 8_000:
        return None, "message_body"
    team_ref = f"teams/{envelope_team}"
    return SlackOwnerDmMessage(
        text,
        team_ref,
        f"{team_ref}/users/{user}",
        f"{team_ref}/channels/{channel}",
        f"{team_ref}/channels/{channel}/messages/{ts}",
    ), "accepted"
