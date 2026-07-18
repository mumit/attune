"""Slack channels and Google Chat spaces as ATTENDED SOURCES (Phase 2 stage 1
of ``docs/future-state.md``, gaps G1/G3 in ``docs/gap-analysis.md``).

Today Slack and Google Chat are conversation surfaces only: the principal's
own allowlisted DMs carry commands (``dispatcher.handle_slack_message`` /
``handle_chat_message``). This module makes selected channels/spaces a SECOND,
opt-in kind of input — a signal source, exactly like a Gmail thread — whose
messages flow cursor -> dispatcher -> triage and land in the attention store
(``orchestrator/attention.py``) as brief/notification material.

**This is the critical distinction every docstring in this module repeats:**
the interaction allowlist (``ATTUNE_SLACK_ALLOWED_USERS`` /
``ATTUNE_CHAT_ALLOWED_USERS``) governs who may COMMAND Attune over a DM.
Source ingestion is unrelated to that gate: every message in a configured
source channel/space is treated as UNTRUSTED SIGNAL regardless of who sent
it — including messages from the principal's own allowlisted account — and
nothing in a source message can ever trigger a write or a conversational
reply (see ``dispatcher.handle_source_message``, which only ever triages and
records; there is no reply path at all here, so a successful prompt
injection inside a source message has no write surface to reach).

Opt-in and off by default: ``ATTUNE_SLACK_SOURCE_CHANNELS`` /
``ATTUNE_CHAT_SOURCE_SPACES`` are empty unless the principal explicitly lists
channel IDs / space resource names (``config.Settings``). Doctor fails fast
(``cli/doctor.check_source_channels``) if either is configured without the
credential needed to read it — the same posture as
``check_channel_routes`` for the conversational routes.

Cursor discipline mirrors ``ingestion/gmail_history.py``/``polling.py``
exactly: the per-channel high-water mark (:class:`~ingestion.state.JsonChatPollState`,
reused — it is already a generic ``{key: {last_seen}}`` store despite its
Chat-flavored name) advances immediately once a bounded page has been LISTED,
decoupled from per-message dispatch success — a dispatch failure enqueues a
durable retry (``ingestion.retry_queue.SqliteRetryQueue``, the same queue
Gmail's path uses, with new ``"slack_source"``/``"chat_source"`` kinds) rather
than blocking or losing the message. First run baselines to "now" and returns
without dispatching anything — never replay channel/space history, same as
every other poller in this package.

Mention detection is deterministic from provider event data, never a model
call:

- **Slack**: ``<@MEMBER_ID>`` literally present in the message text, checked
  against ``ATTUNE_SLACK_ALLOWED_USERS`` — the same identifiers that already
  authenticate the principal's own DMs, reused here as "the principal's Slack
  identity/identities" rather than introducing a second config surface.
- **Google Chat**: a ``USER_MENTION`` annotation (the Chat API's structured
  mention record, distinct from raw ``@name`` text which can't be matched
  reliably) whose ``userMention.user.name`` is in
  ``ATTUNE_CHAT_ALLOWED_USERS``, reused the same way.

Bot self-messages are skipped where the provider marks them (Slack
``bot_id``/``subtype: bot_message``/the bot's own ``user`` id when known;
Chat ``sender.type == "BOT"``, mirroring ``chat_events.process_chat_event``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

# Message text is bounded before it ever reaches a triage prompt or the
# attention store — an unbounded pasted log/thread must not blow up prompt
# size or storage. 2000 chars is generous for a chat message while still
# bounded.
TEXT_CHAR_CAP = 2000

# Bound per poll tick: a channel/space that goes quiet for a long time (or
# the very first catch-up after a config typo is fixed) must not pull an
# unbounded backlog into one tick. Mirrors the spirit of
# ``workspace_polling``'s ``max_results=50``.
SOURCE_POLL_MAX_MESSAGES = 50


@dataclass(frozen=True)
class SourceMessage:
    """One normalized, provenance-tagged chat/Slack message ready for triage.

    ``source`` is ``"slack"`` or ``"google_chat"``. ``channel_ref`` is the
    provider's stable id (Slack channel id / Chat space resource name);
    ``channel_name`` defaults to the same value — resolving a human-friendly
    name needs an extra API call this stage doesn't make (documented
    simplification, not a correctness issue: the ref is still a valid,
    unambiguous label everywhere it's shown). Same convention for
    ``sender_display`` vs. ``sender_ref``.

    ``text`` is truncated to :data:`TEXT_CHAR_CAP` in ``__post_init__`` so
    the bound holds regardless of what the caller passes in — this is the
    one thing about a frozen dataclass that still needs enforcement.
    """

    source: str
    channel_ref: str
    channel_name: str
    sender_ref: str
    sender_display: str
    text: str
    ts: datetime
    thread_ref: str | None
    mentions_principal: bool

    def __post_init__(self) -> None:
        if len(self.text) > TEXT_CHAR_CAP:
            object.__setattr__(self, "text", self.text[:TEXT_CHAR_CAP])


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


def _slack_ts_to_datetime(ts: str) -> datetime:
    """Slack message ``ts`` is a string like ``"1717000000.000100"`` — Unix
    seconds with a fractional part used for tie-breaking, not milliseconds."""
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)


def _slack_ts_now() -> str:
    return f"{datetime.now(timezone.utc).timestamp():.6f}"


def _slack_is_bot_message(raw: dict[str, Any], bot_user_id: str | None) -> bool:
    if raw.get("bot_id"):
        return True
    if raw.get("subtype") == "bot_message":
        return True
    if bot_user_id and raw.get("user") == bot_user_id:
        return True
    return False


def _slack_mentions_principal(
    text: str, principal_member_ids: frozenset[str]
) -> bool:
    return any(f"<@{member_id}>" in text for member_id in principal_member_ids)


def slack_message_to_source(
    raw: dict[str, Any],
    *,
    channel_id: str,
    channel_name: str | None = None,
    principal_member_ids: frozenset[str] = frozenset(),
) -> SourceMessage:
    """Build a :class:`SourceMessage` from one ``conversations.history`` item."""
    text = raw.get("text") or ""
    return SourceMessage(
        source="slack",
        channel_ref=channel_id,
        channel_name=channel_name or channel_id,
        sender_ref=raw.get("user") or raw.get("bot_id") or "",
        sender_display=raw.get("user") or raw.get("username") or "unknown",
        text=text,
        ts=_slack_ts_to_datetime(raw.get("ts") or "0"),
        thread_ref=raw.get("thread_ts"),
        mentions_principal=_slack_mentions_principal(text, principal_member_ids),
    )


def poll_slack_source(
    client: Any,
    channel_id: str,
    state: Any,
    *,
    dispatch: Callable[[SourceMessage], None],
    retry_queue: Any = None,
    bot_user_id: str | None = None,
    channel_name: str | None = None,
    principal_member_ids: frozenset[str] = frozenset(),
    max_messages: int = SOURCE_POLL_MAX_MESSAGES,
) -> int:
    """One Slack source-channel poll tick.

    ``client`` exposes ``conversations_history(channel=, oldest=, limit=,
    inclusive=)`` (a ``slack_sdk.WebClient``, or a fake with the same shape).
    ``state`` is the generic per-key high-water-mark store
    (:class:`~ingestion.state.JsonChatPollState`), keyed ``f"slack:{channel_id}"``
    so Slack channel ids and Chat space names can never collide in a shared
    store.

    First run (no stored cursor): baseline to "now", dispatch nothing, return
    0 — never replay the channel's history.

    Otherwise: list up to ``max_messages`` newer than the stored cursor
    (Slack returns newest-first; this reverses to oldest-first dispatch
    order), advance the cursor to the newest message's ``ts`` IMMEDIATELY —
    before any dispatch is attempted, mirroring ``gmail_history
    .process_notification``'s "the baseline advances once reconciled,
    independent of downstream per-item success" discipline — then dispatch
    each non-bot message. A ``dispatch`` failure enqueues a durable retry
    (``"slack_source"`` kind, ``f"{channel_id}:{ts}"`` as the dedupe key) so
    the message is never silently dropped; without a ``retry_queue`` the
    exception propagates (matches ``dispatcher.handle_gmail_notification``'s
    same fallback for direct/test callers).

    Returns the number of non-bot messages considered (dispatched or
    retried).
    """
    key = f"slack:{channel_id}"
    existing = state.get(key) or {}
    oldest = existing.get("last_seen")
    if oldest is None:
        state.put(key, last_seen=_slack_ts_now())
        return 0

    response = client.conversations_history(
        channel=channel_id, oldest=oldest, limit=max_messages, inclusive=False
    )
    raw_messages = list(reversed(response.get("messages", [])))[:max_messages]

    new_cursor = oldest
    for raw in raw_messages:
        ts = raw.get("ts")
        if ts:
            new_cursor = ts
    if new_cursor != oldest:
        state.put(key, last_seen=new_cursor)

    considered = 0
    for raw in raw_messages:
        if not raw.get("ts"):
            continue
        if _slack_is_bot_message(raw, bot_user_id):
            continue
        considered += 1
        message = slack_message_to_source(
            raw,
            channel_id=channel_id,
            channel_name=channel_name,
            principal_member_ids=principal_member_ids,
        )
        try:
            dispatch(message)
        except Exception as exc:  # noqa: BLE001 — durable retry, never silent
            if retry_queue is None:
                raise
            retry_queue.enqueue(
                "slack_source",
                f"{channel_id}:{raw['ts']}",
                {
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "raw": raw,
                    "principal_member_ids": sorted(principal_member_ids),
                },
                error=type(exc).__name__,
            )
    return considered


# ---------------------------------------------------------------------------
# Google Chat
# ---------------------------------------------------------------------------


def _parse_chat_create_time(raw: str) -> datetime:
    """Parse an RFC 3339 ``createTime``, handling the Z suffix on Python 3.10
    (mirrors ``chat_events._parse_expire_time`` — duplicated rather than
    imported since that helper is private to a different concern)."""
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _chat_is_bot_message(message: dict[str, Any]) -> bool:
    sender = message.get("sender") or {}
    return sender.get("type") == "BOT"


def _chat_mentions_principal(
    message: dict[str, Any], principal_member_ids: frozenset[str]
) -> bool:
    """Keys on structured ``USER_MENTION`` annotations (the Chat API's own
    parsed-mention record), not text pattern matching against ``@name`` —
    raw display names aren't stable or unique enough to match reliably."""
    for annotation in message.get("annotations") or ():
        if annotation.get("type") != "USER_MENTION":
            continue
        user = (annotation.get("userMention") or {}).get("user") or {}
        if user.get("name") in principal_member_ids:
            return True
    return False


def chat_message_to_source(
    message: dict[str, Any],
    *,
    space: str,
    channel_name: str | None = None,
    principal_member_ids: frozenset[str] = frozenset(),
) -> SourceMessage:
    """Build a :class:`SourceMessage` from one ``spaces.messages.list`` item."""
    sender = message.get("sender") or {}
    text = message.get("text") or message.get("argumentText") or ""
    thread = message.get("thread") or {}
    return SourceMessage(
        source="google_chat",
        channel_ref=space,
        channel_name=channel_name or space,
        sender_ref=sender.get("name", ""),
        sender_display=sender.get("displayName") or sender.get("name", "unknown"),
        text=text,
        ts=_parse_chat_create_time(message.get("createTime") or "1970-01-01T00:00:00Z"),
        thread_ref=thread.get("name"),
        mentions_principal=_chat_mentions_principal(message, principal_member_ids),
    )


def poll_chat_source(
    service: Any,
    space: str,
    state: Any,
    *,
    dispatch: Callable[[SourceMessage], None],
    retry_queue: Any = None,
    channel_name: str | None = None,
    principal_member_ids: frozenset[str] = frozenset(),
    max_messages: int = SOURCE_POLL_MAX_MESSAGES,
) -> int:
    """One Chat source-space poll tick — same discipline as
    :func:`poll_slack_source`, over ``service.spaces().messages().list(...)``
    (a ``chat`` v1 API resource, or a fake with the same shape).

    ``state`` is keyed ``f"chat:{space}"`` in the same shared
    :class:`~ingestion.state.JsonChatPollState` store, distinct from both
    Slack source keys and this Chat space's own INTERACTION poll cursor
    (``Runtime.chat_poll_state``, which tracks the DM/command space, not a
    source space, and is a materially different key namespace in practice
    since interaction spaces and source spaces are configured separately).
    """
    key = f"chat:{space}"
    existing = state.get(key) or {}
    last_seen = existing.get("last_seen")
    if last_seen is None:
        state.put(key, last_seen=datetime.now(timezone.utc).isoformat())
        return 0

    response = (
        service.spaces()
        .messages()
        .list(
            parent=space,
            pageSize=max_messages,
            filter=f'createTime > "{last_seen}"',
            orderBy="createTime ASC",
        )
        .execute()
    )
    messages = (response.get("messages") or [])[:max_messages]

    new_cursor = last_seen
    for message in messages:
        create_time = message.get("createTime")
        if create_time:
            new_cursor = create_time
    if new_cursor != last_seen:
        state.put(key, last_seen=new_cursor)

    considered = 0
    for message in messages:
        if not message.get("createTime"):
            continue
        if _chat_is_bot_message(message):
            continue
        considered += 1
        source_message = chat_message_to_source(
            message,
            space=space,
            channel_name=channel_name,
            principal_member_ids=principal_member_ids,
        )
        try:
            dispatch(source_message)
        except Exception as exc:  # noqa: BLE001 — durable retry, never silent
            if retry_queue is None:
                raise
            retry_queue.enqueue(
                "chat_source",
                f"{space}:{message.get('name', message['createTime'])}",
                {
                    "space": space,
                    "channel_name": channel_name,
                    "message": message,
                    "principal_member_ids": sorted(principal_member_ids),
                },
                error=type(exc).__name__,
            )
    return considered
