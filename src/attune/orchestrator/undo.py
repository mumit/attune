"""Undo — a first-class, audited compensating effect (build prompt 31, task 2).

``grep -rn "undo" src/`` used to return zero matches: approving an archive
removed ``INBOX`` with no re-add path, a reschedule patched the event with no
record of the prior time, and the one recovery mechanism was a human doing it
manually in Gmail. Build prompt 30's :class:`~orchestrator.capabilities.Capability`
now carries a real ``compensate`` function for five actions (see that module);
this module is what actually *invokes* one, as its own audited effect rather
than a bypass.

:func:`undo_effect` is the single entry point, called from ``attune undo
<effect-id>`` (``cli/undo_cmd.py``) and, in principle, any future channel
affordance — the effect id IS the decision ledger's ``proposal_id``, which is
always the same value as the workflow's LangGraph ``thread_id`` (see
``ledger._row_from_propose_result``), so the two ids are interchangeable.

Five checks, in order, all fail-closed (refuse rather than guess):

1. A decision ledger row exists for this id at all.
2. It was actually applied (``decision in {"approved", "edited"}`` and
   ``applied_ok``) — nothing to undo for a rejection or a failed apply.
3. It has not already been undone (idempotent: a repeated ``attune undo``
   on the same id is a no-op error, never a second compensating effect).
4. It is within :data:`UNDO_WINDOW` of ``decided_at`` — a bounded window,
   documented not configurable (``draft_approve.UNDO_WINDOW_HOURS``; see
   docs/decisions.md for the 1h justification). A person's approval
   channel is not a request/response cycle, but undo's honesty guarantee
   ("the world hasn't moved much") decays fast, so this window is much
   shorter than the 7-day approval TTL (``orchestrator.pending``).
5. The capability is registered, has a real ``compensate`` function, and
   is not ``irreversible`` — SEND_REPLY (and anything else marked
   irreversible) refuses here, unconditionally, with no bypass.

Only then is ``compensate`` invoked, against the full checkpointed workflow
state (fetched fresh via the graph's own checkpointer — the same state
``apply`` saw, plus whatever ``apply`` itself added, e.g. ``applied_ref``).
Undo's own freshness check lives INSIDE each compensate function (mirroring
how each ``apply_fn`` already embeds its own — see ``capabilities.py``): the
world may have moved since the apply, and a :class:`~.draft_approve.
SourceChangedError` from ``compensate`` is treated exactly like any other
apply-time freshness failure — an honest refusal, not a crash.

Undo may never exceed the authority of the original action (docs/plan-2026-h2.md,
prompt 31's constraints): it only ever REDUCES effect, so it is permitted even
if the rung that authorized the apply has since been revoked — but it is
always audited with the actor who requested it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .draft_approve import UNDO_WINDOW_HOURS, SourceChangedError

UNDO_WINDOW = timedelta(hours=UNDO_WINDOW_HOURS)


class UndoError(Exception):
    """A user-facing reason undo was refused — never a raw exception leak."""


@dataclass(frozen=True)
class UndoResult:
    thread_id: str
    action: str
    domain: str


def undo_effect(
    thread_id: str,
    *,
    graph: Any,
    registry: Any,
    ledger: Any,
    audit_log: Any = None,
    user_id: str | None = None,
    actor: str | None = None,
    now: datetime | None = None,
    window: timedelta = UNDO_WINDOW,
) -> UndoResult:
    """Undo one previously applied effect. Raises :class:`UndoError` (never
    a bare exception) on every refusal path; returns an :class:`UndoResult`
    only once the compensating action has actually run.
    """
    now = now or datetime.now(timezone.utc)

    row = ledger.get(thread_id) if hasattr(ledger, "get") else _find_row(ledger, thread_id)
    if row is None:
        raise UndoError(f"no decision recorded for {thread_id!r}")
    if row.undone:
        raise UndoError(f"{thread_id!r} was already undone")
    if row.decision not in ("approved", "edited"):
        raise UndoError(
            f"nothing to undo — this proposal was {row.decision or 'never decided'}"
        )
    if not row.applied_ok:
        raise UndoError("this proposal was never successfully applied")
    if row.decided_at is None or now - row.decided_at > window:
        hours = int(window.total_seconds() // 3600)
        raise UndoError(
            f"the undo window ({hours}h) has passed since this was applied"
        )

    capability = registry.get(row.action) if registry is not None else None
    if capability is None or capability.irreversible or capability.compensate is None:
        raise UndoError(
            f"{row.action} is irreversible — there is no undo path"
        )

    state = _load_state(graph, thread_id)
    try:
        capability.compensate(state)
    except SourceChangedError as exc:
        _audit(
            audit_log, thread_id, row.domain, user_id, "undo_failed", now=now,
            reason="source_changed", detail=str(exc), actor=actor,
        )
        raise UndoError(f"cannot undo: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — honesty over crash, see module docstring
        _audit(
            audit_log, thread_id, row.domain, user_id, "undo_failed", now=now,
            reason=type(exc).__name__, actor=actor,
        )
        raise UndoError(f"undo failed: {exc}") from exc

    _audit(audit_log, thread_id, row.domain, user_id, "undone", now=now, actor=actor)
    ledger.mark_undone(thread_id, at=now)
    return UndoResult(thread_id=thread_id, action=row.action, domain=row.domain)


def _load_state(graph: Any, thread_id: str) -> dict[str, Any]:
    """The full checkpointed workflow state for ``thread_id`` — the same
    state ``apply`` saw, PLUS whatever ``apply`` itself merged in
    (``applied_ref``, ``undo_available``, …). LangGraph's own checkpointer
    (durable — ``SqliteSaver`` in production, see ``app.py``) is what makes
    this available from a completely separate ``attune undo`` process
    invocation."""
    snapshot = graph.get_state({"configurable": {"thread_id": thread_id}})
    values = getattr(snapshot, "values", None)
    if values is None and isinstance(snapshot, dict):
        values = snapshot.get("values")
    return dict(values or {})


def _find_row(ledger: Any, proposal_id: str) -> Any:
    """Fallback for a ledger substrate with no ``get`` (e.g. a minimal test
    fake) — a full scan of :meth:`~ledger.DecisionLedger.rows`, mirroring
    ``SqliteDecisionLedger.get``'s contract exactly."""
    for row in ledger.rows():
        if row.proposal_id == proposal_id:
            return row
    return None


def _audit(
    audit_log: Any, thread_id: str, domain: "str | None", user_id: "str | None",
    event: str, *, now: "datetime | None" = None, **fields: Any,
) -> None:
    if audit_log is None:
        return
    audit_log.record(
        thread_id=thread_id,
        workflow="draft_approve",
        events=[{
            "event": event,
            "ts": (now or datetime.now(timezone.utc)).isoformat(),
            **fields,
        }],
        domain=domain,
        user_id=user_id,
    )
