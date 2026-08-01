"""The global daily attention budget (build prompt 32, tasks 2 & 3).

Users tolerate roughly three to five unsolicited AI updates per day across
every source combined, then mute, then uninstall — the quantified failure
mode behind OpenAI retiring Pulse's fixed-cadence push (2026-06-17), Google's
Scheduled Actions, and Anthropic's scheduled tasks inside Cowork all
converging on the same lesson. Attune's proactive volume used to be capped
per feature, in ARRIVAL order (``MAX_HOLD_OFFERS_PER_RUN``,
``MAX_DECLINE_PROPOSALS_PER_RUN``, ``MAX_LABEL_PROPOSALS_PER_RUN``,
``MAX_NUDGES_PER_RUN``, plus the weekly autonomy digest and the brief
itself) — on a busy day that's up to a dozen unsolicited messages, allocated
by whichever item happened to arrive first, not by which one matters most.

This module replaces "N per feature per run" with ONE shared allocator and
ONE shared daily counter (:data:`Settings.daily_attention_budget`, default
5): :func:`allocate` ranks every candidate ACROSS features together and
spends whatever's left of today's budget on the highest-ranked ones.

Two rules, both non-negotiable (build prompt 32's own constraints):

1. **URGENT always bypasses the budget.** ``autonomy.PermissionMatrix.
   max_rung`` already treats an URGENT item as structurally different (the
   urgent-interrupt rule caps auto-apply back to PROPOSE regardless of any
   grant); the budget must never become a second way to suppress what that
   rule already insists must interrupt. An urgent candidate is delivered
   unconditionally and never counted against the budget.
2. **Suppression is observable, never silent.** Every non-urgent candidate
   the budget doesn't cover is recorded in the decision ledger as
   ``suppressed_by_budget`` (:func:`record_suppressed`) — this is what keeps
   the coverage metric (build prompt 26) honest: a budget that silently
   dropped work would otherwise look identical to the assistant proposing
   less because it got worse, rather than because it ran out of budget.

Ranking reuses the exact same signals ``brief.py`` already computes rather
than inventing a third scheme (the task's own instruction): urgency/mention,
best counterpart importance tier (mirrors ``brief._best_tier_rank``'s HIGH >
NORMAL > LOW scale), correlated-group size (mirrors
``orchestrator/correlation.py``'s multi-source-beats-single-source rule),
and staleness (older waits first, all else equal).
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Protocol, Sequence

# The importance-tier rank scale, mirroring brief.py's own
# _SPINE_TIER_RANK — kept as a public constant here so callers building
# BudgetCandidates don't need to import brief.py just for these three ints.
TIER_RANK_HIGH = 2
TIER_RANK_NORMAL = 1
TIER_RANK_LOW = 0


@dataclass(frozen=True)
class BudgetCandidate:
    """One candidate unsolicited proactive interruption, from ANY feature
    (label proposal, hold/decline offer, follow-up nudge, routine),
    competing for the shared daily budget.

    ``id`` is whatever the caller uses to key the eventual card/effect
    (thread id, event id, lg_tid) — carried through into the
    ``suppressed_by_budget`` ledger row so a suppressed candidate is
    traceable back to what it was.
    """

    id: str
    domain: str
    action: str
    urgent: bool = False
    tier_rank: int = TIER_RANK_NORMAL
    group_size: int = 1
    staleness_seconds: float = 0.0

    def rank_key(self) -> tuple[int, int, float]:
        """Highest priority first: best tier, then larger correlated
        group, then staler (older) — ties broken by inbound stable order
        (callers should build ``candidates`` in a stable order; ``sorted``
        is stable)."""
        return (self.tier_rank, self.group_size, self.staleness_seconds)


def allocate(
    candidates: Sequence[BudgetCandidate], *, budget: int, spent_today: int,
) -> "tuple[list[BudgetCandidate], list[BudgetCandidate]]":
    """Rank every candidate together and spend what's left of today's
    budget on the highest-ranked ones. Returns ``(delivered, suppressed)``.

    URGENT candidates are ALWAYS in ``delivered`` and never counted against
    ``spent_today``/``budget`` — see the module docstring's rule 1. Ties in
    rank are broken by the input order (a stable sort), so callers that
    want "first-come" as the final tiebreaker just build ``candidates`` in
    that order.
    """
    urgent = [c for c in candidates if c.urgent]
    non_urgent = sorted(
        (c for c in candidates if not c.urgent), key=lambda c: c.rank_key(), reverse=True,
    )
    remaining = max(budget - spent_today, 0)
    delivered_non_urgent = non_urgent[:remaining]
    suppressed = non_urgent[remaining:]
    return urgent + delivered_non_urgent, suppressed


class DailyAttentionBudgetStore(Protocol):
    def spent_today(self, *, today: date) -> int: ...

    def spend(self, n: int, *, today: date) -> None: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS attention_budget (
    day TEXT PRIMARY KEY,
    spent INTEGER NOT NULL DEFAULT 0
)
"""


class SqliteDailyAttentionBudgetStore:
    """One row per calendar day (UTC), the running non-urgent spend for
    that day — deliberately NOT keyed to any one feature, since the whole
    point is one shared counter across every proactive feature."""

    def __init__(self, path: str):
        self._path = path

    def _connect(self) -> sqlite3.Connection:
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self._path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_SCHEMA)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass
        return conn

    def spent_today(self, *, today: date) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT spent FROM attention_budget WHERE day = ?", (today.isoformat(),),
            ).fetchone()
        return row[0] if row else 0

    def spend(self, n: int, *, today: date) -> None:
        if n <= 0:
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO attention_budget (day, spent) VALUES (?, ?)
                ON CONFLICT(day) DO UPDATE SET spent = spent + excluded.spent
                """,
                (today.isoformat(), n),
            )


def record_suppressed(
    ledger: Any, candidate: BudgetCandidate, *, now: datetime, user_id: "str | None" = None,
    audit_log: Any = None,
) -> None:
    """Write one ``suppressed_by_budget`` decision-ledger row (build prompt
    32, task 3's own instruction) — best-effort, same posture as every
    other ledger write in this codebase (a ledger failure must never break
    the decision path it's observing). Excluded from
    ``ledger._DECIDED_STATES`` by construction (that tuple is
    ``("approved", "edited", "rejected")``), so it never contaminates the
    edit-burden average; it IS its own visible row, which is what keeps a
    budget-driven coverage drop distinguishable from a genuine quality
    regression.
    """
    if ledger is None:
        return
    try:
        from .ledger import LedgerRow

        ledger.propose(LedgerRow(
            proposal_id=f"suppressed:{candidate.id}",
            thread_id=candidate.id,
            domain=candidate.domain,
            action=candidate.action,
            proposed_at=now,
            decision="suppressed_by_budget",
            decided_at=now,
        ))
    except Exception:  # noqa: BLE001 — best-effort, see docstring
        import logging

        logging.getLogger(__name__).warning(
            "suppressed_by_budget ledger write failed for %s", candidate.id, exc_info=True,
        )
    if audit_log is not None:
        try:
            audit_log.record(
                thread_id=candidate.id, workflow="attention_budget",
                events=[{
                    "event": "suppressed_by_budget", "ts": now.isoformat(),
                    "domain": candidate.domain, "action": candidate.action,
                }],
                domain=candidate.domain, user_id=user_id,
            )
        except Exception:  # noqa: BLE001 — best-effort
            pass


def spend_budget(
    candidates: Sequence[BudgetCandidate],
    *,
    budget_store: DailyAttentionBudgetStore,
    budget: int,
    ledger: Any = None,
    audit_log: Any = None,
    user_id: "str | None" = None,
    now: "datetime | None" = None,
) -> "list[BudgetCandidate]":
    """The one call site every proactive feature goes through: rank
    ``candidates`` against what's left of TODAY's shared budget, record a
    ``suppressed_by_budget`` ledger row for every one that doesn't make the
    cut, persist the spend, and return the delivered candidates in ranked
    order (urgent first, then by rank) — never in whatever order the
    caller happened to build the list in.
    """
    now = now or datetime.now(timezone.utc)
    today = now.date()
    spent = budget_store.spent_today(today=today)
    delivered, suppressed = allocate(candidates, budget=budget, spent_today=spent)

    non_urgent_delivered = sum(1 for c in delivered if not c.urgent)
    if non_urgent_delivered:
        budget_store.spend(non_urgent_delivered, today=today)
    for candidate in suppressed:
        record_suppressed(ledger, candidate, now=now, user_id=user_id, audit_log=audit_log)
    return delivered
