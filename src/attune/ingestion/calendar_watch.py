"""Calendar push-notification channel lifecycle (design doc 4.3, 4.6).

Unlike Gmail (Pub/Sub) and Chat (Workspace Events, Pub/Sub-capable), Calendar's
push notifications are a **direct HTTPS webhook** — the one exception to "no
inbound port on the credential-holding process" (rule 5). The fix is
architectural, not a compromise: a thin, stateless republisher (Cloud Run /
Cloud Function) receives the webhook, validates it, and republishes onto a
Pub/Sub topic this process pulls from — the same Gmail/Chat pattern, one hop
later. This module only handles the *watch* side: registering and renewing the
notification channel. ``calendar_sync.py`` reconciles a decoded notification
into actual changed events.

A channel expires (Google sets the expiration; commonly weeks out for
Calendar) and, like Gmail's watch, stops delivering *silently* on lapse — so
renewal is a first-class scheduled operation with explicit expiry tracking,
mirroring ``gmail_watch.ensure_watch``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol


class ChannelState(Protocol):
    """Per-calendar persistence: the active channel/resource id and expiry."""

    def get(self, calendar_id: str) -> dict[str, Any] | None: ...
    def put(
        self,
        calendar_id: str,
        *,
        channel_id: str,
        resource_id: str,
        expiration: datetime,
    ) -> None: ...


@dataclass
class ChannelResult:
    calendar_id: str
    channel_id: str
    resource_id: str
    expiration: datetime
    renewed: bool


# Renew when fewer than this many hours remain, same margin as Gmail watches.
RENEW_WHEN_HOURS_LEFT = 48


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _from_epoch_ms(ms: str | int) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


def ensure_calendar_watch(
    calendar: Any,
    state: ChannelState,
    *,
    calendar_id: str = "primary",
    address: str,
    force: bool = False,
    channel_id_factory: Callable[[], str] | None = None,
) -> ChannelResult:
    """Register or renew a Calendar push-notification channel.

    ``calendar`` is a Calendar API client exposing ``events().watch(...)``
    and ``channels().stop(...)``. ``address`` is the thin republisher's HTTPS
    endpoint (rule 5 — this process never listens itself). Renews if the
    stored channel is missing, close to expiry, or ``force`` is set; stops the
    superseded channel afterward so Google doesn't accumulate stale channels
    against the same resource. ``channel_id_factory`` overrides the default
    ``uuid.uuid4``-based id, for deterministic tests.
    """
    existing = state.get(calendar_id)
    if existing and not force:
        exp = existing.get("expiration")
        exp_dt = exp if isinstance(exp, datetime) else _from_epoch_ms(exp)
        if exp_dt - _now() > timedelta(hours=RENEW_WHEN_HOURS_LEFT):
            return ChannelResult(
                calendar_id=calendar_id,
                channel_id=existing["channel_id"],
                resource_id=existing["resource_id"],
                expiration=exp_dt,
                renewed=False,
            )

    make_id = channel_id_factory or (lambda: str(uuid.uuid4()))
    channel_id = make_id()
    body = {"id": channel_id, "type": "web_hook", "address": address}
    resp = calendar.events().watch(calendarId=calendar_id, body=body).execute()

    resource_id = resp["resourceId"]
    expiration = _from_epoch_ms(resp["expiration"])

    if existing and existing.get("channel_id") and existing.get("resource_id"):
        stop_calendar_channel(
            calendar,
            channel_id=existing["channel_id"],
            resource_id=existing["resource_id"],
        )

    state.put(
        calendar_id, channel_id=channel_id, resource_id=resource_id, expiration=expiration
    )
    return ChannelResult(
        calendar_id=calendar_id,
        channel_id=channel_id,
        resource_id=resource_id,
        expiration=expiration,
        renewed=True,
    )


def stop_calendar_channel(calendar: Any, *, channel_id: str, resource_id: str) -> None:
    """Stop a superseded or no-longer-needed notification channel."""
    calendar.channels().stop(
        body={"id": channel_id, "resourceId": resource_id}
    ).execute()
