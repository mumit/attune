"""Tests for the decision ledger (build prompt 26, docs/plan-2026-h2.md P2).

The end-to-end test at the bottom is the one acceptance demands explicitly:
a proposal must carry the EXACT memory ids the retrieve node used.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from attune.orchestrator.autonomy import Rung
from attune.orchestrator.ledger import (
    ContextAttribution,
    LedgerRow,
    SqliteDecisionLedger,
    classify_edit_sections,
    compute_edit_metrics,
    compute_metrics_slice,
    record_decision,
    record_proposal,
    render_metrics_table,
    window_rows,
)

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Edit measurement — deterministic, content-free
# ---------------------------------------------------------------------------


def test_compute_edit_metrics_verbatim_is_zero_distance():
    m = compute_edit_metrics("Hi Alice,\nSure, works.\nBest,\nMe", "Hi Alice,\nSure, works.\nBest,\nMe")
    assert m.char_distance == 0
    assert m.distance_normalized == 0.0
    assert m.sections_changed == ()


def test_compute_edit_metrics_full_rewrite_is_near_one():
    m = compute_edit_metrics("Short reply.", "A completely different and much longer message entirely.")
    assert m.distance_normalized > 0.5


def test_classify_edit_sections_detects_greeting_change():
    proposed = "Hi Bob,\nSure, Thursday works.\nBest,\nMe"
    sent = "Hello Bob,\nSure, Thursday works.\nBest,\nMe"
    changed = classify_edit_sections(proposed, sent)
    assert "greeting" in changed
    assert "body" not in changed
    assert "closing" not in changed


def test_classify_edit_sections_detects_closing_change():
    proposed = "Hi Bob,\nSure, Thursday works.\nBest,\nMe"
    sent = "Hi Bob,\nSure, Thursday works.\nThanks,\nMe"
    changed = classify_edit_sections(proposed, sent)
    assert "closing" in changed
    assert "greeting" not in changed


def test_classify_edit_sections_detects_body_change():
    proposed = "Hi Bob,\nSure, Thursday works.\nBest,\nMe"
    sent = "Hi Bob,\nActually Friday is better.\nBest,\nMe"
    changed = classify_edit_sections(proposed, sent)
    assert "body" in changed
    assert "greeting" not in changed
    assert "closing" not in changed


def test_classify_edit_sections_detects_tone_change():
    proposed = "Hi Bob,\nSure, that works.\nBest,\nMe"
    sent = "Hi Bob,\nSure, that works!\nBest,\nMe"
    changed = classify_edit_sections(proposed, sent)
    assert "tone" in changed


def test_classify_edit_sections_identical_text_is_empty():
    assert classify_edit_sections("same text", "same text") == ()


def test_edit_metrics_never_stores_raw_text():
    """Content-free by construction: EditMetrics has no field that could
    hold the draft or diff text itself."""
    import dataclasses

    fields = {f.name for f in dataclasses.fields(compute_edit_metrics("a", "b"))}
    assert fields == {
        "char_distance", "distance_normalized", "semantic_similarity", "sections_changed",
    }


# ---------------------------------------------------------------------------
# SqliteDecisionLedger — propose / complete / rows
# ---------------------------------------------------------------------------


def _row(**over) -> LedgerRow:
    base = dict(
        proposal_id="gmail:t1:100",
        thread_id="gmail:t1:100",
        domain="mail",
        action="draft_reply",
        proposed_at=NOW,
    )
    base.update(over)
    return LedgerRow(**base)


def test_propose_then_rows_round_trips(tmp_path):
    ledger = SqliteDecisionLedger(str(tmp_path / "ledger.db"))
    ledger.propose(_row(
        context_attribution=ContextAttribution(memory_ids=("m1", "m2")),
        triage_priority="routine", sender_importance_tier="high",
        autonomy_rung_granted=2, autonomy_rung_used=2,
        eligible_item_count=5, batch_id="b1",
    ))
    rows = ledger.rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.thread_id == "gmail:t1:100"
    assert row.context_attribution.memory_ids == ("m1", "m2")
    assert row.triage_priority == "routine"
    assert row.sender_importance_tier == "high"
    assert row.eligible_item_count == 5
    assert row.batch_id == "b1"
    assert row.decision is None


def test_propose_is_idempotent_and_never_clobbers_a_decided_row(tmp_path):
    ledger = SqliteDecisionLedger(str(tmp_path / "ledger.db"))
    ledger.propose(_row())
    ledger.complete(
        "gmail:t1:100", decision="approved", decided_at=NOW + timedelta(seconds=30),
        applied_ok=True,
    )
    # A second propose() for the same proposal_id (a retried dispatcher call)
    # must not wipe out the decision already recorded.
    ledger.propose(_row())
    row = ledger.rows()[0]
    assert row.decision == "approved"


def test_complete_computes_time_to_decision(tmp_path):
    ledger = SqliteDecisionLedger(str(tmp_path / "ledger.db"))
    ledger.propose(_row(proposed_at=NOW))
    ledger.complete(
        "gmail:t1:100", decision="approved",
        decided_at=NOW + timedelta(seconds=45), applied_ok=True,
    )
    row = ledger.rows()[0]
    assert row.time_to_decision_seconds == 45.0
    assert row.applied_ok is True


def test_complete_with_edit_computes_content_free_metrics(tmp_path):
    ledger = SqliteDecisionLedger(str(tmp_path / "ledger.db"))
    ledger.propose(_row())
    ledger.complete(
        "gmail:t1:100", decision="edited", decided_at=NOW + timedelta(seconds=10),
        proposed_text="Hi Bob,\nSure, Thursday works.\nBest,\nMe",
        final_text="Hello Bob,\nSure, Thursday works.\nBest,\nMe",
        applied_ok=True,
    )
    row = ledger.rows()[0]
    assert row.edit_char_distance is not None and row.edit_char_distance > 0
    assert 0.0 < row.edit_distance_normalized <= 1.0
    assert "greeting" in row.edit_sections_changed


def test_complete_on_unproposed_id_is_a_noop(tmp_path):
    ledger = SqliteDecisionLedger(str(tmp_path / "ledger.db"))
    ledger.complete("never-proposed", decision="approved")  # must not raise
    assert ledger.rows() == []


def test_mark_undone(tmp_path):
    ledger = SqliteDecisionLedger(str(tmp_path / "ledger.db"))
    ledger.propose(_row())
    ledger.complete("gmail:t1:100", decision="approved", applied_ok=True)
    ledger.mark_undone("gmail:t1:100", at=NOW + timedelta(hours=1))
    row = ledger.rows()[0]
    assert row.undone is True
    assert row.undone_at is not None


def test_rows_filters_by_since_domain_action(tmp_path):
    ledger = SqliteDecisionLedger(str(tmp_path / "ledger.db"))
    ledger.propose(_row(proposal_id="a", thread_id="a", domain="mail", action="draft_reply", proposed_at=NOW))
    ledger.propose(_row(proposal_id="b", thread_id="b", domain="calendar", action="create_hold", proposed_at=NOW + timedelta(days=1)))

    assert len(ledger.rows(domain="mail")) == 1
    assert len(ledger.rows(action="create_hold")) == 1
    assert len(ledger.rows(since=NOW + timedelta(hours=12))) == 1


def test_ledger_file_is_chmodded_owner_only(tmp_path):
    import os

    path = tmp_path / "ledger.db"
    ledger = SqliteDecisionLedger(str(path))
    ledger.propose(_row())
    assert (os.stat(path).st_mode & 0o777) == 0o600


def test_never_touches_disk_until_first_write(tmp_path):
    """Lazy initialization, same convention as every other local state store
    in this codebase (JsonAttentionStore, SqliteRetryQueue, ...)."""
    path = tmp_path / "ledger.db"
    SqliteDecisionLedger(str(path))
    assert not path.exists()


# ---------------------------------------------------------------------------
# Aggregation — the north-star metric + mandatory coverage
# ---------------------------------------------------------------------------


def test_compute_metrics_slice_exact(tmp_path):
    ledger = SqliteDecisionLedger(str(tmp_path / "ledger.db"))
    # Two approved-unedited (batch of 4 eligible items), one edited (own
    # batch of 1), one rejected (own batch of 1).
    ledger.propose(_row(proposal_id="a", thread_id="a", eligible_item_count=4, batch_id="batch1"))
    ledger.complete("a", decision="approved", decided_at=NOW + timedelta(seconds=10), applied_ok=True)
    ledger.propose(_row(proposal_id="b", thread_id="b", eligible_item_count=4, batch_id="batch1"))
    ledger.complete("b", decision="approved", decided_at=NOW + timedelta(seconds=20), applied_ok=True)
    ledger.propose(_row(proposal_id="c", thread_id="c", eligible_item_count=1, batch_id="batch2"))
    ledger.complete(
        "c", decision="edited", decided_at=NOW + timedelta(seconds=30),
        proposed_text="Hi Bob,\nSure.\nBest,\nMe", final_text="Hello Bob,\nSure.\nBest,\nMe",
        applied_ok=True,
    )
    ledger.propose(_row(proposal_id="d", thread_id="d", eligible_item_count=1, batch_id="batch3"))
    ledger.complete("d", decision="rejected", decided_at=NOW + timedelta(seconds=5))

    rows = ledger.rows()
    m = compute_metrics_slice(rows)
    assert m.proposals == 4
    assert m.decided == 4
    # coverage = 4 proposals / (4 + 1 + 1) distinct-batch eligible items
    assert m.coverage == pytest.approx(4 / 6)
    assert m.clean_approval_rate == pytest.approx(2 / 4)
    # edit burden over SENT proposals only (approved+edited): a,b at 0.0, c > 0
    assert m.edit_burden is not None and m.edit_burden > 0
    assert m.p50_time_to_decision_seconds == pytest.approx(15.0)  # median of 10,20,30,5


def test_coverage_dedupes_by_batch_not_summed_per_row(tmp_path):
    """Three proposals from the SAME batch of 10 eligible items must not
    triple-count the denominator."""
    ledger = SqliteDecisionLedger(str(tmp_path / "ledger.db"))
    for i in range(3):
        ledger.propose(_row(
            proposal_id=f"t{i}", thread_id=f"t{i}",
            eligible_item_count=10, batch_id="one-batch",
        ))
    m = compute_metrics_slice(ledger.rows())
    assert m.coverage == pytest.approx(3 / 10)


def test_metrics_slice_with_no_rows_is_all_none():
    m = compute_metrics_slice([])
    assert m.proposals == 0
    assert m.edit_burden is None
    assert m.coverage is None
    assert m.triage_escalation_rate is None


def test_triage_escalation_rate_is_distinct_from_autonomy_escalation_rate(tmp_path):
    """Build prompt 33, task 7: cascade triage's escalation rate (cheap ->
    strong model) must be its own metric, never conflated with the
    pre-existing ``escalation_rate`` (autonomy-rung fallback to a human
    interrupt) — the two measure unrelated things."""
    ledger = SqliteDecisionLedger(str(tmp_path / "ledger.db"))
    ledger.propose(_row(
        proposal_id="a", thread_id="a", triage_escalated=True,
        autonomy_rung_granted=3, autonomy_rung_used=3,
    ))
    ledger.propose(_row(
        proposal_id="b", thread_id="b", triage_escalated=False,
        autonomy_rung_granted=3, autonomy_rung_used=1,  # an autonomy escalation
    ))
    m = compute_metrics_slice(ledger.rows())

    assert m.triage_escalation_rate == pytest.approx(1 / 2)
    assert m.escalation_rate == pytest.approx(1 / 2)  # unrelated, happens to also be 1/2
    # A row that never recorded a triage decision (e.g. calendar) must not
    # count toward the denominator at all.
    ledger.propose(_row(proposal_id="c", thread_id="c"))
    m2 = compute_metrics_slice(ledger.rows())
    assert m2.triage_escalation_rate == pytest.approx(1 / 2)


def test_render_metrics_table_shows_triage_escalation_column(tmp_path):
    ledger = SqliteDecisionLedger(str(tmp_path / "ledger.db"))
    ledger.propose(_row(triage_escalated=True, eligible_item_count=1, batch_id="b1"))
    ledger.complete("gmail:t1:100", decision="approved", applied_ok=True)
    text = render_metrics_table(ledger.rows(), window_days=14, now=NOW)
    assert "triage_esc%" in text


def test_render_metrics_table_always_shows_coverage(tmp_path):
    ledger = SqliteDecisionLedger(str(tmp_path / "ledger.db"))
    ledger.propose(_row(eligible_item_count=2, batch_id="b1"))
    ledger.complete("gmail:t1:100", decision="approved", applied_ok=True)
    text = render_metrics_table(ledger.rows(), window_days=14, now=NOW)
    assert "coverage" in text
    lines = [line for line in text.splitlines() if line.strip().startswith("(all)")]
    assert lines  # the overall row rendered


def test_render_metrics_table_slices_by_domain_and_tier(tmp_path):
    ledger = SqliteDecisionLedger(str(tmp_path / "ledger.db"))
    ledger.propose(_row(
        proposal_id="a", thread_id="a", domain="mail", action="draft_reply",
        sender_importance_tier="high", eligible_item_count=1, batch_id="ba",
    ))
    ledger.complete("a", decision="approved", applied_ok=True)
    ledger.propose(_row(
        proposal_id="b", thread_id="b", domain="calendar", action="create_hold",
        sender_importance_tier="low", eligible_item_count=1, batch_id="bb",
    ))
    ledger.complete("b", decision="approved", applied_ok=True)
    text = render_metrics_table(ledger.rows(), window_days=14, now=NOW)
    assert "by domain:" in text and "mail" in text and "calendar" in text
    assert "by sender importance tier:" in text and "high" in text and "low" in text


def test_window_rows_excludes_outside_window():
    old = _row(proposed_at=NOW - timedelta(days=30))
    recent = _row(proposal_id="r", thread_id="r", proposed_at=NOW - timedelta(days=1))
    scoped = window_rows([old, recent], window_days=14, now=NOW)
    assert scoped == [recent]


def test_render_metrics_table_empty_window_message():
    text = render_metrics_table([], window_days=14, now=NOW)
    assert "No proposals in this window." in text


# ---------------------------------------------------------------------------
# record_proposal / record_decision — the graph-state extraction helpers
# ---------------------------------------------------------------------------


def test_record_proposal_and_decision_are_noop_without_a_ledger():
    # Must not raise when ledger=None (not yet wired at a call site).
    record_proposal(None, thread_id="t", domain="mail", action="draft_reply", result={})
    record_decision(None, thread_id="t", result={})


def test_record_proposal_extracts_context_attribution(tmp_path):
    ledger = SqliteDecisionLedger(str(tmp_path / "ledger.db"))
    result = {
        "retrieved_memory_ids": ["m1", "m2"],
        "base_priority": "routine",
        "audit_events": [{
            "event": "autonomy_gate", "ts": NOW.isoformat(),
            "action": "draft_reply", "domain": "mail",
            "max_rung": int(Rung.PROPOSE), "routed_to": "approve",
            "scope_context": {"priority": "routine", "tier": "high", "matched_rung": int(Rung.PROPOSE)},
            "autonomy_rung_granted": int(Rung.PROPOSE),
            "scope_matched": False,
            "profile_reason": "3 approvals in a row",
        }],
    }
    record_proposal(
        ledger, thread_id="gmail:t1:100", domain="mail", action="draft_reply",
        result=result, model_id="gpt-test", eligible_item_count=3, batch_id="b1",
        now=NOW,
    )
    row = ledger.rows()[0]
    assert row.context_attribution.memory_ids == ("m1", "m2")
    assert row.triage_priority == "routine"
    assert row.sender_importance_tier == "high"
    assert row.base_priority == "routine"
    assert row.autonomy_rung_granted == int(Rung.PROPOSE)
    assert row.autonomy_rung_used == int(Rung.PROPOSE)
    assert row.profile_reason == "3 approvals in a row"
    assert row.model_id == "gpt-test"
    assert row.eligible_item_count == 3
    assert row.batch_id == "b1"
    assert row.decision is None  # paused at interrupt, not yet decided


def test_record_proposal_completes_immediately_for_auto_applied_path(tmp_path):
    """When the graph ran straight through (auto_apply, no interrupt), the
    initial invoke's result already carries a decision — record_proposal
    must complete the row in the same call, since there is no resume."""
    ledger = SqliteDecisionLedger(str(tmp_path / "ledger.db"))
    result = {
        "retrieved_memory_ids": [],
        "decision": "approved",
        "final_text": "Sure.",
        "proposed_draft": "Sure.",
        "applied_ref": "draft-1",
        "audit_events": [{
            "event": "autonomy_gate", "ts": NOW.isoformat(),
            "action": "draft_reply", "domain": "mail",
            "max_rung": int(Rung.ACT_NOTIFY), "routed_to": "auto_apply",
            "scope_context": {"priority": None, "tier": None, "matched_rung": int(Rung.ACT_NOTIFY)},
        }],
    }
    record_proposal(
        ledger, thread_id="gmail:t2:100", domain="mail", action="draft_reply",
        result=result, now=NOW,
    )
    row = ledger.rows()[0]
    assert row.decision == "approved"
    assert row.applied_ok is True


# ---------------------------------------------------------------------------
# End to end: one proposal through the real graph, asserting the ledger row
# carries the EXACT memory ids the retrieve node used (the acceptance test).
# ---------------------------------------------------------------------------


def test_end_to_end_ledger_row_carries_exact_retrieved_memory_ids(tmp_path):
    langgraph = pytest.importorskip("langgraph")
    from langgraph.types import Command

    from attune.memory.base import MemoryRecord, MemoryStore

    class _FakeStore(MemoryStore):
        def add(self, messages, *, user_id, metadata=None, infer=True):
            return []

        def search(self, query, *, user_id, limit=8, min_score=None):
            return [
                MemoryRecord(id="mem-alpha", text="prefers short replies", score=0.9),
                MemoryRecord(id="mem-beta", text="always CCs manager", score=0.8),
            ]

        def get_all(self, *, user_id, limit=100):
            return []

        def delete(self, memory_id):
            pass

    class _FakeClient:
        def chat_completions_create(self, **kwargs):
            class _Msg:
                content = "Sure, that works."

            class _Choice:
                message = _Msg()

            class _Resp:
                choices = [_Choice()]

            return _Resp()

    from attune.orchestrator import build_draft_approve_graph

    graph = build_draft_approve_graph(client=_FakeClient(), store=_FakeStore())
    ledger = SqliteDecisionLedger(str(tmp_path / "ledger.db"))
    thread_id = "gmail:t-e2e:100"
    cfg = {"configurable": {"thread_id": thread_id}}

    state = {
        "user_id": "mumit", "domain": "mail", "action": "draft_reply",
        "incoming_ref": "msg-1", "incoming_summary": "Can we reschedule?",
        "sender": "vendor@example.com", "subject": "Reschedule?",
        "priority": "routine", "audit_events": [], "iteration_count": 0,
    }
    result = graph.invoke(state, cfg)
    assert "__interrupt__" in result  # paused for human approval

    record_proposal(
        ledger, thread_id=thread_id, domain="mail", action="draft_reply",
        result=result, now=NOW,
    )

    row = ledger.rows()[0]
    assert row.context_attribution.memory_ids == ("mem-alpha", "mem-beta")

    # Now resume with an edit, and confirm the decision completes the SAME row.
    final = graph.invoke(
        Command(resume={"decision": "edited", "text": "Sure, Thursday works."}), cfg
    )
    record_decision(ledger, thread_id=thread_id, result=final, actor="mumit", now=NOW + timedelta(minutes=2))

    completed = ledger.rows()[0]
    assert completed.context_attribution.memory_ids == ("mem-alpha", "mem-beta")
    assert completed.decision == "edited"
    assert completed.actor_ref == "mumit"
    assert completed.time_to_decision_seconds == pytest.approx(120.0)
    assert completed.edit_sections_changed  # something changed, deterministically classified
