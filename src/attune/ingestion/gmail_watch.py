"""Gmail watch lifecycle (design doc 4.3, 4.6).

A Gmail push-notification watch expires after at most 7 days; Google recommends
re-calling ``users.watch`` once per day. Miss the window and notifications stop
*silently* — no error, the app just goes quiet — which is the single most common
failure of this integration. So watch renewal is treated as a first-class,
scheduled operation with explicit expiry tracking, not an afterthought.

This module is transport-agnostic: it drives the watch via an injected Gmail
client (the direct-OAuth service, later) and persists the resulting baseline via
an injected state store. That keeps the credential-holding logic testable and
lets the actual google-api-python-client be supplied from outside.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol


class WatchState(Protocol):
    """Per-mailbox persistence: the last historyId baseline and watch expiry."""

    def get(self, email: str) -> dict[str, Any] | None: ...
    def put(self, email: str, *, history_id: str, expiration: datetime) -> None: ...


@dataclass
class WatchResult:
    email: str
    history_id: str
    expiration: datetime
    renewed: bool


# Renew when fewer than this many hours remain. With a daily cron and a 7-day
# expiry there's ample margin, but we renew proactively rather than at the edge.
RENEW_WHEN_HOURS_LEFT = 48


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _from_epoch_ms(ms: str | int) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


def ensure_watch(
    gmail: Any,
    state: WatchState,
    *,
    email: str = "me",
    topic: str,
    label_ids: list[str] | None = None,
    force: bool = False,
) -> WatchResult:
    """Register or renew a Gmail watch, storing the new baseline.

    Called daily by a scheduler. Renews if the stored watch is missing, close to
    expiry, or ``force`` is set. ``gmail`` is a Gmail API client exposing
    ``users().watch(...).execute()``; ``topic`` is the fully-qualified Pub/Sub
    topic (``projects/<p>/topics/<t>``) that must already exist with Gmail
    granted publish permission.
    """
    existing = state.get(email)
    if existing and not force:
        exp = existing.get("expiration")
        exp_dt = exp if isinstance(exp, datetime) else _from_epoch_ms(exp)
        if exp_dt - _now() > timedelta(hours=RENEW_WHEN_HOURS_LEFT):
            return WatchResult(email, existing["history_id"], exp_dt, renewed=False)

    body: dict[str, Any] = {
        "topicName": topic,
        "labelIds": label_ids or ["INBOX"],
        "labelFilterBehavior": "INCLUDE",
    }
    resp = gmail.users().watch(userId=email, body=body).execute()
    history_id = str(resp["historyId"])
    expiration = _from_epoch_ms(resp["expiration"])
    state.put(email, history_id=history_id, expiration=expiration)
    return WatchResult(email, history_id, expiration, renewed=True)
