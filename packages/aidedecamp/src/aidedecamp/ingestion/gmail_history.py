"""Process a Gmail push notification into a deduped list of changed threads
(design doc 4.3, 4.6).

The Pub/Sub payload is deceptively simple and has three traps this module
handles explicitly, because each is a silent-data-loss bug otherwise:

1. **The notification's historyId is the LATEST, not the change point.** You must
   reconcile from your *stored* historyId (``startHistoryId``) up to now, not
   from the one in the payload. So we always read the baseline from state and
   advance it only after a successful reconcile.

2. **A stale historyId returns 404.** historyIds older than ~7 days expire; when
   ``history.list`` 404s, the only recovery is a full re-sync. We surface that as
   a distinct outcome so the caller re-baselines rather than silently missing
   mail.

3. **Messages are duplicated across history records.** The same message/thread
   can appear in multiple history entries and multiple change types. We dedupe by
   threadId so downstream drafting runs once per thread.

Transport-agnostic by design: this takes an already-decoded notification dict.
The Pub/Sub HTTP receipt + base64url decode happens in a thin republisher
OUTSIDE the credential-holding process (the no-inbound-port rule, design 4.6);
``decode_pubsub_message`` is provided for that republisher's convenience.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any

from .gmail_watch import WatchState


class HistoryExpired(Exception):
    """Raised when history.list reports the startHistoryId is too old (404).

    Signals the caller to perform a full re-sync and re-baseline the watch."""


@dataclass
class MailboxChanges:
    email: str
    new_history_id: str
    thread_ids: list[str] = field(default_factory=list)  # deduped, changed threads


def decode_pubsub_message(message: dict[str, Any]) -> dict[str, Any]:
    """Decode a Pub/Sub push ``message`` into ``{emailAddress, historyId}``.

    Intended for the external republisher. ``message['data']`` is base64url JSON.
    """
    raw = base64.urlsafe_b64decode(message["data"])
    return json.loads(raw)


def _is_404(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "resp", None), "status", None
    )
    return str(status) == "404"


def process_notification(
    gmail: Any,
    state: WatchState,
    notification: dict[str, Any],
) -> MailboxChanges:
    """Reconcile a decoded notification into deduped changed thread ids.

    ``notification`` is ``{"emailAddress": ..., "historyId": ...}``. Uses the
    STORED baseline as startHistoryId, dedupes by threadId, and advances the
    stored baseline only on success. Raises :class:`HistoryExpired` on 404.
    """
    email = notification["emailAddress"]
    stored = state.get(email)
    if not stored:
        # No baseline to reconcile from; caller must do a full sync + watch.
        raise HistoryExpired(f"No stored historyId for {email}; full sync required")

    start_history_id = stored["history_id"]

    try:
        thread_ids = _collect_thread_ids(gmail, email, start_history_id)
    except Exception as exc:  # noqa: BLE001
        if _is_404(exc):
            raise HistoryExpired(
                f"startHistoryId {start_history_id} expired for {email}; "
                "full re-sync required"
            ) from exc
        raise

    new_history_id = str(notification.get("historyId", start_history_id))
    # Advance the baseline so the next notification reconciles from here.
    exp = stored.get("expiration")
    state.put(email, history_id=new_history_id, expiration=exp)

    return MailboxChanges(
        email=email, new_history_id=new_history_id, thread_ids=thread_ids
    )


# A messagesAdded record with any of these labels is the OWNER acting (their
# own sent mail, or draft-save churn), not inbound signal (review finding #3):
# reacting to it means triaging your own words and drafting replies to
# yourself. Only genuinely inbound additions count as thread changes.
_OWN_ACTIVITY_LABELS = frozenset({"SENT", "DRAFT"})


def _collect_thread_ids(gmail: Any, email: str, start_history_id: str) -> list[str]:
    """Page through history.list, collecting changed threadIds, deduped, order-
    preserving. Looks at messagesAdded (new mail); SENT/DRAFT-labeled
    additions — the owner's own activity — are excluded, so a thread counts
    as changed only when at least one inbound message arrived."""
    seen: set[str] = set()
    ordered: list[str] = []
    page_token: str | None = None

    while True:
        req = gmail.users().history().list(
            userId=email,
            startHistoryId=start_history_id,
            historyTypes=["messageAdded"],
            pageToken=page_token,
        )
        resp = req.execute()
        for record in resp.get("history", []):
            for added in record.get("messagesAdded", []):
                msg = added.get("message", {})
                if _OWN_ACTIVITY_LABELS & set(msg.get("labelIds") or ()):
                    continue
                tid = msg.get("threadId")
                if tid and tid not in seen:
                    seen.add(tid)
                    ordered.append(tid)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return ordered
