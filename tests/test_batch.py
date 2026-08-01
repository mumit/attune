"""Tests for orchestrator/batch.py — batch approval cards (build prompt 31,
tasks 4 & 5).

Covers the acceptance criterion: "approve all" over 5 items produces 5
audited applies, 5 ledger rows, and 5 freshness checks — and a repeated
click applies nothing further. Also covers grouping (never fewer than 2,
never mixing capabilities) and the "never a truncated list" rendering rule.
"""

from __future__ import annotations

from datetime import datetime, timezone

from attune.orchestrator.batch import (
    group_pending_by_capability,
    render_batch_card,
    resolve_batch_approve_all,
)
from attune.orchestrator.pending import PendingApproval, STATUS_PENDING, STATUS_RESOLVED

T0 = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


def _entry(lg_tid, *, domain="mail", action="label", subject=None, sender=None, status=STATUS_PENDING):
    return PendingApproval(
        lg_tid=lg_tid, source_ref=lg_tid, domain=domain, posted_at=T0,
        status=status, sender=sender, subject=subject or lg_tid, action=action,
    )


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def test_groups_entries_sharing_domain_and_action():
    entries = [
        _entry("archive:t1"), _entry("archive:t2"), _entry("archive:t3"),
    ]
    groups = group_pending_by_capability(entries)
    assert len(groups) == 1
    assert groups[0].domain == "mail" and groups[0].action == "label"
    assert groups[0].thread_ids == ("archive:t1", "archive:t2", "archive:t3")


def test_never_mixes_different_capabilities_into_one_group():
    entries = [
        _entry("archive:t1", action="label"),
        _entry("archive:t2", action="label"),
        _entry("gmail:t3", action="draft_reply"),
        _entry("gmail:t4", action="draft_reply"),
    ]
    groups = group_pending_by_capability(entries)
    assert len(groups) == 2
    by_action = {g.action: g.thread_ids for g in groups}
    assert by_action["label"] == ("archive:t1", "archive:t2")
    assert by_action["draft_reply"] == ("gmail:t3", "gmail:t4")


def test_single_pending_item_never_forms_a_batch():
    entries = [_entry("archive:t1")]
    assert group_pending_by_capability(entries) == []


def test_entries_with_no_action_never_join_a_group():
    """A card posted before build prompt 31 (or any caller that never set
    ``action``) carries ``action=None`` and must not be silently batched
    with anything — an unknown capability can't be safely grouped."""
    entries = [
        PendingApproval(
            lg_tid="legacy:t1", source_ref="t1", domain="mail",
            posted_at=T0, status=STATUS_PENDING, action=None,
        ),
        PendingApproval(
            lg_tid="legacy:t2", source_ref="t2", domain="mail",
            posted_at=T0, status=STATUS_PENDING, action=None,
        ),
    ]
    assert group_pending_by_capability(entries) == []


def test_non_pending_entries_are_excluded_from_grouping():
    entries = [
        _entry("archive:t1"), _entry("archive:t2"),
        _entry("archive:t3", status=STATUS_RESOLVED),
    ]
    groups = group_pending_by_capability(entries)
    assert len(groups) == 1
    assert groups[0].thread_ids == ("archive:t1", "archive:t2")


# ---------------------------------------------------------------------------
# Rendering — never a truncated list
# ---------------------------------------------------------------------------


def test_render_batch_card_names_every_item_individually():
    entries = [
        _entry("archive:t1", subject="Weekly digest", sender="news@x.com"),
        _entry("archive:t2", subject="Promo blast", sender="ads@x.com"),
        _entry("archive:t3", subject="Another one", sender="spam@x.com"),
    ]
    groups = group_pending_by_capability(entries)
    text = render_batch_card(groups[0])

    for entry in entries:
        assert entry.subject in text
        assert entry.sender in text
    assert "3 pending label proposals" in text
    assert "accept / edit / respond / ignore" in text.lower()


# ---------------------------------------------------------------------------
# "Approve all" — never one aggregate effect (task 4/5's own constraint)
# ---------------------------------------------------------------------------


class _FakePending:
    """Minimal per-item claim semantics: a claim succeeds exactly once per
    thread_id, mirroring JsonPendingApprovals.claim's real contract."""

    def __init__(self):
        self.claimed: set[str] = set()
        self.claim_calls: list[str] = []

    def claim(self, thread_id, *, actor=None):
        self.claim_calls.append(thread_id)
        if thread_id in self.claimed:
            return False
        self.claimed.add(thread_id)
        return True


def test_approve_all_over_five_items_produces_five_individually_audited_applies():
    thread_ids = [f"gmail:t{i}:100" for i in range(5)]
    pending = _FakePending()
    audit_events: list[dict] = []
    ledger_rows: list[str] = []
    freshness_checks: list[str] = []

    def fake_resume_workflow(graph, thread_id, decision, text=None, *, pending, audit_log, user_id, actor, ledger, store):
        claimed = pending.claim(thread_id, actor=actor)
        if claimed is False:
            return {"decision": decision, "apply_error": "already_handled"}
        # each item does its OWN freshness check (stand-in for the real
        # apply_fn's _check_freshness_mail/_check_freshness_calendar_event)
        freshness_checks.append(thread_id)
        audit_log.record(thread_id=thread_id, workflow="draft_approve", events=[{"event": "applied"}])
        ledger_rows.append(thread_id)
        return {"decision": decision, "applied_ref": f"{thread_id}-ref"}

    class _Audit:
        def record(self, **kw):
            audit_events.append(kw)

    import attune.orchestrator.draft_approve as draft_approve_module
    orig = draft_approve_module.resume_workflow
    draft_approve_module.resume_workflow = fake_resume_workflow
    try:
        results = resolve_batch_approve_all(
            thread_ids, graph=object(), pending=pending, audit_log=_Audit(),
            user_id="me", actor="U1",
        )
    finally:
        draft_approve_module.resume_workflow = orig

    assert len(results) == 5
    assert all(r.get("applied_ref") for r in results)
    assert len(audit_events) == 5
    assert len(ledger_rows) == 5
    assert len(freshness_checks) == 5
    assert freshness_checks == thread_ids  # every item's own check ran


def test_approve_all_repeated_click_applies_nothing_further():
    thread_ids = [f"gmail:t{i}:100" for i in range(5)]
    pending = _FakePending()
    applied: list[str] = []

    def fake_resume_workflow(graph, thread_id, decision, text=None, *, pending, audit_log, user_id, actor, ledger, store):
        claimed = pending.claim(thread_id, actor=actor)
        if claimed is False:
            return {"decision": decision, "apply_error": "already_handled", "approval_already_handled": True}
        applied.append(thread_id)
        return {"decision": decision, "applied_ref": f"{thread_id}-ref"}

    import attune.orchestrator.draft_approve as draft_approve_module
    orig = draft_approve_module.resume_workflow
    draft_approve_module.resume_workflow = fake_resume_workflow
    try:
        first = resolve_batch_approve_all(thread_ids, graph=object(), pending=pending)
        second = resolve_batch_approve_all(thread_ids, graph=object(), pending=pending)
    finally:
        draft_approve_module.resume_workflow = orig

    assert len(applied) == 5           # only the first pass actually applied
    assert all(r.get("applied_ref") for r in first)
    assert all(r.get("approval_already_handled") for r in second)


def test_resolve_batch_approve_all_uses_injected_resume_for_tests():
    calls = []
    resolve_batch_approve_all(
        ["a", "b", "c"], resume=lambda tid: calls.append(tid) or {"ok": tid}
    )
    assert calls == ["a", "b", "c"]
