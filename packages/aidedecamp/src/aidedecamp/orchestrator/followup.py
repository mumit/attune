"""Quiet-thread follow-up nudges (design 3.3, roadmap prompt 15).

"You haven't heard back from Marcus in 4 days on the contract redline — want
a follow-up drafted?" This is the system's first genuinely *proactive*
action offer, so it enters the autonomy ladder exactly like everything else:

**A nudge is an approval card for a follow-up draft.** Each candidate starts
the existing draft-approve graph (``action=Action.FOLLOW_UP`` — its own
action type in the matrix, granted at PROPOSE by default, separately
grantable/revocable from DRAFT_REPLY) and the normal gate → interrupt → card
flow does everything else: approval materializes a Gmail draft via the apply
node, edits feed correction capture, ignored cards decay via the pending
sweep. No new approval surface, no new autonomy path (rule 3 — the nudge
*offers*; only the human approval turns it into a draft).

Candidates come from ``brief.find_quiet_threads`` — deliberately the single
source of quiet-thread truth (the brief renders the same list) — filtered
through a cooldown state so a thread is nudged at most once per
``ADC_NUDGE_COOLDOWN_DAYS``, capped per run. A proactive feature that spams
is worse than none (design 8.1's Lindy critique): the caps and cooldowns are
hard limits.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from ..brief import find_quiet_threads
from ..connectors.base import EmailThread, WorkspaceConnector

MAX_NUDGES_PER_RUN = 3
DEFAULT_MIN_AGE_DAYS = 4
DEFAULT_COOLDOWN_DAYS = 7


class NudgeState(Protocol):
    def last_nudged(self, thread_id: str) -> datetime | None: ...

    def record_nudge(self, thread_id: str, *, at: datetime) -> None: ...


class JsonNudgeState:
    """File-backed cooldown record: ``{thread_id: {nudged_at: iso}}``."""

    def __init__(self, path: str):
        self._path = path

    def last_nudged(self, thread_id: str) -> datetime | None:
        raw = self._load().get(thread_id)
        if raw is None:
            return None
        return datetime.fromisoformat(raw["nudged_at"])

    def record_nudge(self, thread_id: str, *, at: datetime) -> None:
        data = self._load()
        data[thread_id] = {"nudged_at": at.astimezone(timezone.utc).isoformat()}
        self._save(data)

    def _load(self) -> dict[str, Any]:
        if not os.path.exists(self._path):
            return {}
        with open(self._path) as fh:
            return json.load(fh)

    def _save(self, data: dict[str, Any]) -> None:
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self._path, "w") as fh:
            json.dump(data, fh)


def find_nudge_candidates(
    connector: WorkspaceConnector,
    nudge_state: NudgeState,
    *,
    user_email: str,
    now: datetime | None = None,
    min_age_days: int = DEFAULT_MIN_AGE_DAYS,
    cooldown_days: int = DEFAULT_COOLDOWN_DAYS,
    max_candidates: int = MAX_NUDGES_PER_RUN,
) -> list[EmailThread]:
    """Quiet threads worth nudging about: reuses the brief's quiet-thread
    truth, then drops anything nudged within the cooldown, capped hard."""
    now = now or datetime.now(timezone.utc)
    cooldown = timedelta(days=cooldown_days)
    candidates: list[EmailThread] = []
    for thread in find_quiet_threads(
        connector, user_email=user_email, now=now, min_age_days=min_age_days
    ):
        # No counterparty (an owner-only sent thread) -> nobody to nudge; a
        # follow-up draft would be addressed to the owner (finding #3).
        reply_to = getattr(thread, "reply_to", "")
        if not reply_to or user_email.lower() in reply_to.lower():
            continue
        last = nudge_state.last_nudged(thread.thread_id)
        if last is not None and now - last < cooldown:
            continue
        candidates.append(thread)
        if len(candidates) >= max_candidates:
            break
    return candidates


@dataclass
class NudgeResult:
    thread: EmailThread
    lg_tid: str


def run_follow_up_nudges(
    app_ctx: Any,
    connector: WorkspaceConnector,
    nudge_state: NudgeState,
    *,
    user_email: str,
    user_id: str,
    post_approval: Callable[..., None],
    pending: Any = None,
    audit_log: Any = None,
    now: datetime | None = None,
    min_age_days: int = DEFAULT_MIN_AGE_DAYS,
    cooldown_days: int = DEFAULT_COOLDOWN_DAYS,
    notify: Callable[[str], None] | None = None,
) -> list[NudgeResult]:
    """One nudge run: start a FOLLOW_UP draft-approve workflow per candidate
    and post its card (titled as a nudge). The cooldown is recorded after a
    successful post, so a failed run retries next time rather than silently
    consuming the thread's nudge budget.
    """
    now = now or datetime.now(timezone.utc)
    candidates = find_nudge_candidates(
        connector, nudge_state, user_email=user_email, now=now,
        min_age_days=min_age_days, cooldown_days=cooldown_days,
    )

    results: list[NudgeResult] = []
    for thread in candidates:
        age_days = (
            (now - thread.last_message_at).days
            if thread.last_message_at is not None
            else min_age_days
        )
        lg_tid = f"followup:{thread.thread_id}:{now:%Y%m%d}"
        incoming_summary = (
            f"You sent the last message on this thread {age_days} days ago and "
            "have received no reply. Draft a brief, polite follow-up nudging "
            "for a response.\n\n"
            f"Subject: {thread.subject}\n"
            f"Thread participants include: {thread.from_addr}\n"
            f"Last message snippet: {thread.snippet}"
        )
        state = {
            "incoming_summary": incoming_summary,
            "incoming_ref": thread.thread_id,
            "user_id": user_id,
            "action": "follow_up",
            "domain": "mail",
            "iteration_count": 0,
            "audit_events": [],
        }
        result = app_ctx.graph.invoke(
            state, {"configurable": {"thread_id": lg_tid}}
        )

        if audit_log is not None:
            audit_log.record(
                thread_id=lg_tid,
                workflow="followup",
                events=[{
                    "event": "nudge_offered",
                    "ts": now.isoformat(),
                    "gmail_thread_id": thread.thread_id,
                    "quiet_days": age_days,
                }] + list(result.get("audit_events", [])),
                domain="mail",
                user_id=user_id,
            )

        from ..dispatcher import _auto_rung, _handle_auto_applied

        rung = _auto_rung(result)
        if rung is not None:
            _handle_auto_applied(
                result, rung,
                action="follow_up", domain="mail",
                describe=f'drafted a follow-up on "{thread.subject}"',
                lg_tid=lg_tid, user_id=user_id,
                notify=notify, audit_log=audit_log,
            )
        else:
            post_approval(
                lg_tid,
                result.get("proposed_draft") or "",
                result.get("retrieved_memories") or None,
                title=f"Follow-up nudge — no reply in {age_days}d: {thread.subject}",
            )
            if pending is not None:
                pending.register(
                    lg_tid=lg_tid, source_ref=thread.thread_id,
                    domain="mail", posted_at=now,
                )
        nudge_state.record_nudge(thread.thread_id, at=now)
        results.append(NudgeResult(thread=thread, lg_tid=lg_tid))
    return results
