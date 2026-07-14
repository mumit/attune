"""Concrete, file-backed persistence for the ingestion state protocols.

``gmail_watch.WatchState`` and ``chat_events.SubscriptionState`` are Protocols
— every test in this codebase injects a dict-backed fake for them, and until
now nothing implemented a real one, so ``runtime.build_runtime()`` had no
default to fall back to. These are deliberately the simplest thing that
satisfies each protocol: one JSON file per state kind, read fully on ``get``,
rewritten fully on ``put``. Fine at this scale (single-mailbox, single-space
deployments); swap for something else if that stops being true, the same way
``JsonlAuditLog`` is a swappable stand-in behind ``AuditLog``.

The two classes are NOT interchangeable despite the similar shape: each
serializes ``expiration`` the way its consuming module's read path expects —
``gmail_watch.ensure_watch`` reconstructs a non-datetime expiration via
``_from_epoch_ms`` (epoch milliseconds), while ``chat_events.ensure_subscription``
reconstructs one via ``_parse_expire_time`` (an ISO-8601 string). Getting this
wrong wouldn't fail loudly — it would silently mis-time renewal.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any


def _load(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path) as fh:
        return json.load(fh)


def _save(path: str, data: dict[str, Any]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(data, fh)


class JsonGmailWatchState:
    """Persists Gmail watch baselines: ``{email: {history_id, expiration}}``.

    ``expiration`` is stored as epoch milliseconds — the form
    ``gmail_watch.ensure_watch``'s ``_from_epoch_ms`` expects when the stored
    value isn't already a ``datetime``.
    """

    def __init__(self, path: str):
        self._path = path

    def get(self, email: str) -> dict[str, Any] | None:
        return _load(self._path).get(email)

    def put(self, email: str, *, history_id: str, expiration: datetime) -> None:
        data = _load(self._path)
        data[email] = {
            "history_id": history_id,
            "expiration": int(expiration.timestamp() * 1000),
        }
        _save(self._path, data)


class JsonChatSubscriptionState:
    """Persists Chat Workspace Events subscriptions:
    ``{space: {subscription_name, expiration}}``.

    ``expiration`` is stored as an ISO-8601 string — the form
    ``chat_events.ensure_subscription``'s ``_parse_expire_time`` expects when
    the stored value isn't already a ``datetime``.
    """

    def __init__(self, path: str):
        self._path = path

    def get(self, space: str) -> dict[str, Any] | None:
        return _load(self._path).get(space)

    def put(self, space: str, *, subscription_name: str, expiration: datetime) -> None:
        data = _load(self._path)
        data[space] = {
            "subscription_name": subscription_name,
            "expiration": expiration.astimezone(timezone.utc).isoformat(),
        }
        _save(self._path, data)


class JsonCalendarChannelState:
    """Persists Calendar notification channels:
    ``{calendar_id: {channel_id, resource_id, expiration}}``.

    ``expiration`` is stored as epoch milliseconds — the form
    ``calendar_watch.ensure_calendar_watch``'s ``_from_epoch_ms`` expects when
    the stored value isn't already a ``datetime`` (same convention as Gmail's
    watch state; Calendar's ``events.watch`` returns expiration the same way).
    """

    def __init__(self, path: str):
        self._path = path

    def get(self, calendar_id: str) -> dict[str, Any] | None:
        return _load(self._path).get(calendar_id)

    def put(
        self, calendar_id: str, *, channel_id: str, resource_id: str, expiration: datetime
    ) -> None:
        data = _load(self._path)
        data[calendar_id] = {
            "channel_id": channel_id,
            "resource_id": resource_id,
            "expiration": int(expiration.timestamp() * 1000),
        }
        _save(self._path, data)


class JsonChatPollState:
    """Persists the Chat poll high-water mark: ``{space: {last_seen}}``.

    ``last_seen`` is the RFC 3339 createTime of the newest message already
    dispatched (poll mode only — see ``ingestion/polling.py``); an opaque
    string round-tripped as-is, like the Calendar sync token.
    """

    def __init__(self, path: str):
        self._path = path

    def get(self, space: str) -> dict[str, Any] | None:
        return _load(self._path).get(space)

    def put(self, space: str, *, last_seen: str) -> None:
        data = _load(self._path)
        data[space] = {"last_seen": last_seen}
        _save(self._path, data)


class JsonCalendarSyncState:
    """Persists Calendar incremental sync tokens: ``{calendar_id: {sync_token}}``.

    No datetime involved, so serialization is trivial — the token is an
    opaque string round-tripped as-is.
    """

    def __init__(self, path: str):
        self._path = path

    def get(self, calendar_id: str) -> dict[str, Any] | None:
        return _load(self._path).get(calendar_id)

    def put(self, calendar_id: str, *, sync_token: str) -> None:
        data = _load(self._path)
        data[calendar_id] = {"sync_token": sync_token}
        _save(self._path, data)
