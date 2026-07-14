"""Timer-driven ingestion — poll mode (roadmap prompt 09).

Push ingestion needs four Pub/Sub topic+subscription pairs, a deployed Cloud
Run republisher, and watch/subscription lifecycle management *before the
first event flows*. But every reconciliation primitive in this codebase is
already trigger-agnostic: Gmail's ``history.list`` walks from a stored
baseline, Calendar's sync token diffs on any tick, and Chat messages can be
listed by create time. A timer can drive all three — outbound-only, exactly
as rule-5-clean as pull subscriptions — which turns "a weekend of GCP
plumbing" into "OAuth + go".

**The dispatcher seam does not move.** Each poll step synthesizes the same
decoded shapes the push path produces, so ``dispatcher.py`` never learns
which mode fed it:

- :func:`poll_gmail_step` → the ``{"emailAddress", "historyId"}`` dict a
  Pub/Sub notification carries (only when the mailbox actually advanced;
  one cheap ``getProfile`` call per tick).
- :func:`calendar_poll_notification` → the synthetic notification that
  triggers a sync-token reconcile (the handler ignores the body anyway;
  empty ticks are cheap by construction).
- :func:`poll_chat_step` → Workspace-Events-shaped message payloads from
  ``spaces.messages.list`` filtered by a stored high-water mark. The caller
  persists the new mark only after successful dispatch.

First-run behavior mirrors push mode's watch registration: baseline "now"
and move forward — never replay the mailbox/space history.

What poll mode cannot do: Chat **card-click interactions** still require the
republisher (Google POSTs them; there is nothing to poll). Approve/reject/
edit work fully over Slack (Socket Mode) in poll mode; Chat cards need the
interaction subscription configured or push mode.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Mirrors chat_events._MESSAGE_CREATED (pinned by a test) so synthesized
# events decode through process_chat_event unchanged.
_MESSAGE_CREATED = "google.workspace.chat.message.v1.created"


def poll_gmail_step(
    gmail_service: Any, watch_state: Any, *, email: str = "me"
) -> dict[str, Any] | None:
    """One Gmail poll tick: synthesize a push-shaped notification iff the
    mailbox's historyId advanced past the stored baseline.

    First run (no baseline): store the current historyId and return ``None``
    — start from now, don't replay the mailbox. ``watch_state`` is the same
    store push mode uses; its ``expiration`` field is inert in poll mode
    (nothing to renew).
    """
    profile = gmail_service.users().getProfile(userId=email).execute()
    current = str(profile.get("historyId", ""))
    if not current:
        return None

    existing = watch_state.get(email)
    if existing is None or not existing.get("history_id"):
        watch_state.put(
            email, history_id=current, expiration=datetime.now(timezone.utc)
        )
        return None

    if int(current) <= int(existing["history_id"]):
        return None
    return {"emailAddress": email, "historyId": current}


def calendar_poll_notification() -> dict[str, Any]:
    """The synthetic notification a Calendar poll tick feeds the dispatcher.

    ``handle_calendar_notification`` only ever uses a notification as a
    signal to reconcile against the stored sync token, so the body carries
    nothing but an honest label."""
    return {"resource_state": "poll"}


def poll_chat_step(
    chat_service: Any,
    *,
    space: str,
    last_seen: str | None,
    page_size: int = 25,
) -> tuple[list[dict[str, Any]], str | None]:
    """One Chat poll tick: list messages newer than the high-water mark and
    synthesize Workspace-Events-shaped payloads for each.

    Returns ``(events, new_mark)``. The caller persists ``new_mark`` only
    after every event dispatched successfully — a crash mid-batch redelivers
    on the next tick rather than dropping messages. First run
    (``last_seen`` is None): return no events and "now" as the mark — start
    from now, don't replay the space.
    """
    if last_seen is None:
        return [], datetime.now(timezone.utc).isoformat()

    response = (
        chat_service.spaces()
        .messages()
        .list(
            parent=space,
            pageSize=page_size,
            filter=f'createTime > "{last_seen}"',
            orderBy="createTime ASC",
        )
        .execute()
    )
    messages = response.get("messages", [])
    events = [{"type": _MESSAGE_CREATED, "message": m} for m in messages]
    new_mark = messages[-1].get("createTime") if messages else None
    return events, new_mark
