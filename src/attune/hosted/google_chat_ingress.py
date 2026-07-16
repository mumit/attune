"""Strict normalization of verified Google Chat owner-DM link events."""

from __future__ import annotations

import re
from dataclasses import dataclass

_LINK_MESSAGE = re.compile(r"^/link ([A-Za-z0-9_-]{43})$")
_ACTOR_REF = re.compile(r"^users/[A-Za-z0-9._-]{1,180}$")
_SPACE_REF = re.compile(r"^spaces/[A-Za-z0-9_-]{1,180}$")


@dataclass(frozen=True, repr=False)
class GoogleChatOwnerDmLink:
    link_code: str
    actor_ref: str
    destination_ref: str

    def __repr__(self) -> str:
        return "GoogleChatOwnerDmLink(link_code=<redacted>, actor_ref=<redacted>, destination_ref=<redacted>)"


def decode_owner_dm_link(event: object) -> GoogleChatOwnerDmLink | None:
    return decode_owner_dm_link_diagnostic(event)[0]


def decode_owner_dm_link_diagnostic(
    event: object,
) -> tuple[GoogleChatOwnerDmLink | None, str]:
    if not isinstance(event, dict) or event.get("type") != "MESSAGE":
        return None, "event_envelope"
    user = event.get("user")
    space = event.get("space")
    message = event.get("message")
    if not all(isinstance(value, dict) for value in (user, space, message)):
        return None, "identity_envelope"
    sender = message.get("sender")
    message_space = message.get("space")
    if not isinstance(sender, dict) or not isinstance(message_space, dict):
        return None, "identity_envelope"
    actor_ref = user.get("name")
    destination_ref = space.get("name")
    if (
        user.get("type") != "HUMAN"
        or sender.get("type") != "HUMAN"
        or sender.get("name") != actor_ref
        or space.get("type") != "DIRECT_MESSAGE"
        or message_space.get("type") != "DIRECT_MESSAGE"
        or message_space.get("name") != destination_ref
        or not isinstance(actor_ref, str)
        or not _ACTOR_REF.fullmatch(actor_ref)
        or not isinstance(destination_ref, str)
        or not _SPACE_REF.fullmatch(destination_ref)
    ):
        return None, "actor_space_binding"
    # Google supplies argumentText with Chat-app mentions removed.  Prefer that
    # provider-canonical command body when non-empty; text can contain the app
    # mention even in a direct-message interaction. Protobuf string fields do
    # not distinguish omission from an empty value, so retain the exact-text
    # fallback in either case.
    argument_text = message.get("argumentText")
    text = (
        argument_text
        if isinstance(argument_text, str) and argument_text
        else message.get("text")
    )
    if not isinstance(text, str):
        return None, "command_body"
    matched = _LINK_MESSAGE.fullmatch(text)
    if matched is None:
        return None, "command_body"
    return GoogleChatOwnerDmLink(matched.group(1), actor_ref, destination_ref), "accepted"
