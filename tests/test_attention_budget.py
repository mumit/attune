"""Tests for orchestrator/attention_budget.py — the global daily attention
budget (build prompt 32, tasks 2 & 3).

Covers the acceptance criterion: a budget of 3 with 10 candidates delivers
the 3 highest-ranked, records 7 ``suppressed_by_budget`` ledger rows, and an
URGENT eleventh candidate is still delivered.
"""

from __future__ import annotations

from datetime import datetime, timezone

from attune.orchestrator.attention_budget import (
    BudgetCandidate,
    SqliteDailyAttentionBudgetStore,
    allocate,
    spend_budget,
)
from attune.orchestrator.ledger import SqliteDecisionLedger

T0 = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)


def _candidates(n: int, *, tier_rank: int = 1) -> list[BudgetCandidate]:
    return [
        BudgetCandidate(
            id=f"c{i}", domain="mail", action="label", tier_rank=tier_rank + i,
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# allocate — pure ranking/allocation
# ---------------------------------------------------------------------------


def test_allocate_delivers_only_the_highest_ranked_up_to_budget():
    candidates = _candidates(10)
    delivered, suppressed = allocate(candidates, budget=3, spent_today=0)

    assert len(delivered) == 3
    assert len(suppressed) == 7
    # Highest tier_rank values (9, 8, 7 -> ids c9, c8, c7) win.
    assert {c.id for c in delivered} == {"c9", "c8", "c7"}
    assert {c.id for c in suppressed} == {f"c{i}" for i in range(7)}


def test_allocate_urgent_candidate_always_delivered_and_uncounted():
    candidates = _candidates(10) + [
        BudgetCandidate(id="urgent1", domain="mail", action="draft_reply", urgent=True),
    ]
    delivered, suppressed = allocate(candidates, budget=3, spent_today=0)

    assert "urgent1" in {c.id for c in delivered}
    assert len(delivered) == 4  # 3 budgeted + 1 urgent, never counted against budget
    assert len(suppressed) == 7
    assert "urgent1" not in {c.id for c in suppressed}


def test_allocate_respects_already_spent_budget():
    candidates = _candidates(5)
    delivered, suppressed = allocate(candidates, budget=3, spent_today=2)
    assert len(delivered) == 1
    assert len(suppressed) == 4


def test_allocate_never_delivers_negative_remaining():
    candidates = _candidates(5)
    delivered, suppressed = allocate(candidates, budget=3, spent_today=10)
    assert delivered == []
    assert len(suppressed) == 5


# ---------------------------------------------------------------------------
# SqliteDailyAttentionBudgetStore
# ---------------------------------------------------------------------------


def test_budget_store_tracks_spend_per_day(tmp_path):
    store = SqliteDailyAttentionBudgetStore(str(tmp_path / "budget.db"))
    today = T0.date()
    assert store.spent_today(today=today) == 0
    store.spend(2, today=today)
    store.spend(1, today=today)
    assert store.spent_today(today=today) == 3

    tomorrow = today.replace(day=today.day + 1)
    assert store.spent_today(today=tomorrow) == 0  # a new day starts fresh


# ---------------------------------------------------------------------------
# spend_budget — end-to-end: allocation + ledger recording + persisted spend
# ---------------------------------------------------------------------------


def test_spend_budget_end_to_end_records_seven_suppressed_rows_and_urgent_still_delivered(tmp_path):
    budget_store = SqliteDailyAttentionBudgetStore(str(tmp_path / "budget.db"))
    ledger = SqliteDecisionLedger(str(tmp_path / "ledger.db"))

    candidates = _candidates(10) + [
        BudgetCandidate(id="urgent1", domain="mail", action="draft_reply", urgent=True),
    ]
    delivered = spend_budget(
        candidates, budget_store=budget_store, budget=3, ledger=ledger, now=T0,
    )

    assert len(delivered) == 4  # 3 budgeted highest-ranked + the urgent one
    assert "urgent1" in {c.id for c in delivered}

    rows = ledger.rows()
    suppressed_rows = [r for r in rows if r.decision == "suppressed_by_budget"]
    assert len(suppressed_rows) == 7
    assert {r.thread_id for r in suppressed_rows} == {f"c{i}" for i in range(7)}
    # The urgent candidate and the 3 delivered ones never got a suppressed row.
    assert "urgent1" not in {r.thread_id for r in suppressed_rows}

    # The shared daily counter reflects only the non-urgent spend (3), not
    # the urgent delivery.
    assert budget_store.spent_today(today=T0.date()) == 3


def test_spend_budget_a_second_call_the_same_day_respects_remaining_budget(tmp_path):
    budget_store = SqliteDailyAttentionBudgetStore(str(tmp_path / "budget.db"))
    ledger = SqliteDecisionLedger(str(tmp_path / "ledger.db"))

    first_batch = _candidates(3)
    delivered_first = spend_budget(
        first_batch, budget_store=budget_store, budget=3, ledger=ledger, now=T0,
    )
    assert len(delivered_first) == 3  # budget fully spent

    second_batch = [BudgetCandidate(id="later1", domain="calendar", action="create_hold")]
    delivered_second = spend_budget(
        second_batch, budget_store=budget_store, budget=3, ledger=ledger,
        now=T0.replace(hour=14),
    )
    assert delivered_second == []
    rows = ledger.rows()
    assert any(r.thread_id == "later1" and r.decision == "suppressed_by_budget" for r in rows)


def test_suppressed_rows_excluded_from_edit_burden_and_never_marked_decided(tmp_path):
    from attune.orchestrator.ledger import compute_metrics_slice

    budget_store = SqliteDailyAttentionBudgetStore(str(tmp_path / "budget.db"))
    ledger = SqliteDecisionLedger(str(tmp_path / "ledger.db"))
    spend_budget(
        _candidates(5), budget_store=budget_store, budget=1, ledger=ledger, now=T0,
    )

    rows = ledger.rows()
    metrics = compute_metrics_slice(rows)
    # None of the 4 suppressed rows count as "decided" (approved/edited/rejected).
    assert metrics.decided == 0
    assert metrics.edit_burden is None
