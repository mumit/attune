"""Google Chat space event subscriptions via the Workspace Events API.

Subscribes to Chat space message events so the process receives @mentions and
direct messages as Pub/Sub payloads — decoded by the thin republisher outside
this process, consistent with the no-inbound-port rule (design 4.6).

Subscription lifecycle mirrors Gmail watch management:
- Subscriptions under user-scoped OAuth expire in ≤ 7 days.
- ``ensure_subscription`` renews proactively at < RENEW_WHEN_HOURS_LEFT hours
  remaining, treating a missing or near-expiry subscription the same way
  ``ensure_watch`` treats a lapsed Gmail watch.

This module is transport-agnostic: ``workspace_events`` is an injected API
service (``googleapiclient.discovery.build("workspaceevents", "v1", ...)``) so
the logic is testable without a live Google connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol


class SubscriptionState(Protocol):
    """Per-space persistence: subscription resource name and expiry."""

    def get(self, space: str) -> dict[str, Any] | None: ...
    def put(
        self, space: str, *, subscription_name: str, expiration: datetime
    ) -> None: ...


@dataclass
class SubscriptionResult:
    space: str
    subscription_name: str
    expiration: datetime
    renewed: bool


@dataclass
class ChatMessage:
    """A decoded, bot-safe Chat space message ready for the dispatcher."""

    space: str         # "spaces/AAAA…"
    sender: str        # "users/CCCC…"
    text: str          # message text, @mention prefix stripped (argumentText)
    message_name: str = ""  # "spaces/A/messages/B" for reference


# Renew when fewer than this many hours remain, same margin as Gmail watches.
RENEW_WHEN_HOURS_LEFT = 48

# The Workspace Events API event type for new Chat messages.
_MESSAGE_CREATED = "google.workspace.chat.message.v1.created"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_expire_time(s: str) -> datetime:
    """Parse an RFC 3339 string, handling the Z suffix on Python 3.10."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def ensure_subscription(
    workspace_events: Any,
    state: SubscriptionState,
    *,
    space: str,
    topic: str,
    force: bool = False,
) -> SubscriptionResult:
    """Create or renew a Workspace Events subscription for Chat space messages.

    ``workspace_events`` exposes ``subscriptions().create(...).execute()``.
    ``space`` is a resource name (``"spaces/AAAA…"``). ``topic`` is the
    fully-qualified Pub/Sub topic (``"projects/<p>/topics/<t>"``) that must
    already exist with the Workspace Events API granted publish permission.
    """
    existing = state.get(space)
    if existing and not force:
        exp = existing.get("expiration")
        if isinstance(exp, str):
            exp = _parse_expire_time(exp)
        if isinstance(exp, datetime) and exp - _now() > timedelta(hours=RENEW_WHEN_HOURS_LEFT):
            return SubscriptionResult(
                space=space,
                subscription_name=existing["subscription_name"],
                expiration=exp,
                renewed=False,
            )

    body = {
        "targetResource": f"//chat.googleapis.com/{space}",
        "eventTypes": [_MESSAGE_CREATED],
        "notificationEndpoint": {"pubsubTopic": topic},
    }
    resp = workspace_events.subscriptions().create(body=body).execute()

    subscription_name = resp.get("name", "")
    raw_exp = resp.get("expireTime", "")
    expiration = (
        _parse_expire_time(raw_exp) if raw_exp else _now() + timedelta(days=7)
    )

    state.put(space, subscription_name=subscription_name, expiration=expiration)
    return SubscriptionResult(
        space=space,
        subscription_name=subscription_name,
        expiration=expiration,
        renewed=True,
    )


def process_chat_event(event: dict[str, Any]) -> ChatMessage | None:
    """Extract a :class:`ChatMessage` from a decoded Workspace Events payload.

    Returns ``None`` when:
    - the event type is not a message-created event, or
    - the message sender is a bot (prevents self-reply loops).
    """
    event_type = event.get("type") or event.get("eventType", "")
    if _MESSAGE_CREATED not in str(event_type):
        return None

    message = event.get("message", {})
    sender = message.get("sender", {})
    if sender.get("type") == "BOT":
        return None

    # argumentText is the message text with the @mention prefix stripped.
    text = (message.get("argumentText") or message.get("text") or "").strip()
    space_name = (
        message.get("space", {}).get("name")
        or event.get("space", {}).get("name", "")
    )

    return ChatMessage(
        space=space_name,
        sender=sender.get("name", ""),
        text=text,
        message_name=message.get("name", ""),
    )
