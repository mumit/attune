"""Reconcile a Calendar push notification into changed event ids (design 4.3, 4.6).

Calendar's webhook carries almost no payload — just headers identifying the
channel/resource and a resource state (``sync``/``exists``/``not_exists``).
Finding out *what* changed requires an incremental sync,
``events.list(syncToken=...)``, against the STORED baseline token — the same
shape as Gmail's historyId reconciliation, with the same two traps:

1. **No stored sync token** (first notification, or after a resync) means a
   full sync is required first — surfaced as :class:`SyncExpired`, the same
   shape as Gmail's "no baseline" case.
2. **An expired sync token returns 410 Gone** — also surfaced as
   :class:`SyncExpired`, requiring the same full-resync recovery path.

Unlike Gmail, where re-registering the watch happens to also return a fresh
historyId baseline, Calendar's sync token is entirely independent of the
channel/watch lifecycle: renewing the watch (``calendar_watch.py``) does NOT
give you a new sync token. Recovering from :class:`SyncExpired` always means
calling :func:`full_calendar_sync`, not renewing the watch.

Cancelled events show up in the incremental list with ``status="cancelled"``;
they're included in the returned ids so the caller can react to
cancellations, not just new/changed events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class SyncState(Protocol):
    """Per-calendar persistence: the last-seen incremental sync token."""

    def get(self, calendar_id: str) -> dict[str, Any] | None: ...
    def put(self, calendar_id: str, *, sync_token: str) -> None: ...


class SyncExpired(Exception):
    """Raised when there's no stored sync token, or the stored one is stale (410).

    Signals the caller to run :func:`full_calendar_sync` to re-baseline."""


@dataclass
class CalendarChanges:
    calendar_id: str
    next_sync_token: str
    event_ids: list[str] = field(default_factory=list)  # includes cancelled events


def decode_calendar_headers(headers: dict[str, str]) -> dict[str, str]:
    """Extract the ``X-Goog-*`` notification headers into a clean dict.

    Intended for the external republisher (rule 5): the actual HTTPS webhook
    receipt happens outside this process; this documents/normalizes the
    header shape once a decoded notification is forwarded here.
    """
    return {
        "channel_id": headers.get("X-Goog-Channel-ID", ""),
        "resource_id": headers.get("X-Goog-Resource-ID", ""),
        "resource_state": headers.get("X-Goog-Resource-State", ""),
        "message_number": headers.get("X-Goog-Message-Number", ""),
    }


def _is_410(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "resp", None), "status", None
    )
    return str(status) == "410"


def process_calendar_notification(
    calendar: Any,
    state: SyncState,
    calendar_id: str = "primary",
) -> CalendarChanges:
    """Reconcile one notification into deduped changed/cancelled event ids.

    Uses the STORED sync token as the baseline, dedupes event ids, and
    advances the stored token only on success. Raises :class:`SyncExpired`
    when no baseline exists or the stored token has expired (410) — the
    caller must call :func:`full_calendar_sync` to re-baseline.
    """
    stored = state.get(calendar_id)
    if not stored or not stored.get("sync_token"):
        raise SyncExpired(f"No stored sync token for {calendar_id}; full sync required")

    try:
        event_ids, next_token = _collect_changes(
            calendar, calendar_id, stored["sync_token"]
        )
    except Exception as exc:  # noqa: BLE001
        if _is_410(exc):
            raise SyncExpired(
                f"Sync token expired for {calendar_id}; full re-sync required"
            ) from exc
        raise

    state.put(calendar_id, sync_token=next_token)
    return CalendarChanges(
        calendar_id=calendar_id, next_sync_token=next_token, event_ids=event_ids
    )


def full_calendar_sync(
    calendar: Any, state: SyncState, calendar_id: str = "primary"
) -> CalendarChanges:
    """Perform a full sync to obtain a fresh baseline token.

    Called after :func:`process_calendar_notification` raises
    :class:`SyncExpired` (first-ever sync, or a 410 recovery)."""
    event_ids, next_token = _collect_changes(calendar, calendar_id, sync_token=None)
    state.put(calendar_id, sync_token=next_token)
    return CalendarChanges(
        calendar_id=calendar_id, next_sync_token=next_token, event_ids=event_ids
    )


def _collect_changes(
    calendar: Any, calendar_id: str, sync_token: str | None
) -> tuple[list[str], str]:
    """Page through events.list, collecting deduped, order-preserving event
    ids (including cancelled ones), and capture the final page's
    nextSyncToken."""
    seen: set[str] = set()
    ordered: list[str] = []
    page_token: str | None = None
    next_sync_token = ""

    while True:
        kwargs: dict[str, Any] = {"calendarId": calendar_id, "pageToken": page_token}
        if sync_token:
            kwargs["syncToken"] = sync_token
        resp = calendar.events().list(**kwargs).execute()

        for item in resp.get("items", []):
            eid = item.get("id")
            if eid and eid not in seen:
                seen.add(eid)
                ordered.append(eid)

        page_token = resp.get("nextPageToken")
        if resp.get("nextSyncToken"):
            next_sync_token = resp["nextSyncToken"]
        if not page_token:
            break

    return ordered, next_sync_token
