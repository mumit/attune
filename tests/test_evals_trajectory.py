"""Trajectory-level assertions (build prompt 27, task 4) over REAL
``build_draft_approve_graph`` runs — every violation-detecting test here
constructs a scenario where the invariant genuinely is (or would be)
broken, proving the assertions are load-bearing rather than vacuous."""

from __future__ import annotations

import pytest

from attune.memory.base import MemoryRecord, MemoryStore
from attune.orchestrator import (
    Action,
    Domain,
    PermissionMatrix,
    Rung,
    build_draft_approve_graph,
)
from attune.evals.trajectory import (
    RecordingMemoryStore,
    assert_autonomy_rung_respected,
    assert_capability_selected,
    assert_freshness_checked_before_apply,
    assert_no_write_on_read_only,
    assert_retrieval_requested_score_floor,
    run_trajectory_assertions,
)


class _FakeStore(MemoryStore):
    def add(self, messages, *, user_id, metadata=None, infer=True):
        return []

    def search(self, query, *, user_id, limit=8, min_score=None):
        return [MemoryRecord(id="mem-1", text="prefers short replies", score=0.9)]

    def get_all(self, *, user_id, limit=100):
        return []

    def delete(self, memory_id):
        pass


class _FakeClient:
    def chat_completions_create(self, **kwargs):
        class _M:
            pass

        m = _M()
        m.content = "Sure, that works."

        class _C:
            pass

        c = _C()
        c.message = m

        class _R:
            pass

        r = _R()
        r.choices = [c]
        return r


def _base_state(**overrides):
    state = {
        "user_id": "mumit", "domain": "mail", "action": "draft_reply",
        "incoming_ref": "msg-1", "incoming_summary": "Can we reschedule?",
        "sender": "vendor@example.com", "subject": "Reschedule?",
        "priority": "routine", "audit_events": [], "iteration_count": 0,
    }
    state.update(overrides)
    return state


def test_auto_apply_happy_path_has_no_violations():
    pytest.importorskip("langgraph")
    matrix = PermissionMatrix().grant(Action.DRAFT_REPLY, Domain.MAIL, Rung.ACT_NOTIFY)
    graph = build_draft_approve_graph(client=_FakeClient(), store=_FakeStore(), matrix=matrix)
    cfg = {"configurable": {"thread_id": "t-auto"}}
    result = graph.invoke(_base_state(), cfg)

    assert result.get("decision") == "approved"
    violations = run_trajectory_assertions(
        "t-auto", result, expected_action="draft_reply", expected_domain="mail",
    )
    assert violations == []


def test_approve_route_with_human_decision_has_no_violations():
    pytest.importorskip("langgraph")
    from langgraph.types import Command

    matrix = PermissionMatrix()  # no grant -> falls through to approve
    graph = build_draft_approve_graph(client=_FakeClient(), store=_FakeStore(), matrix=matrix)
    cfg = {"configurable": {"thread_id": "t-approve"}}
    graph.invoke(_base_state(), cfg)
    result = graph.invoke(Command(resume={"decision": "approved"}), cfg)

    violations = run_trajectory_assertions(
        "t-approve", result, expected_action="draft_reply", expected_domain="mail",
    )
    assert violations == []


def test_wrong_capability_is_flagged():
    result = {"action": "send_reply", "domain": "mail", "audit_events": []}
    violations = assert_capability_selected(
        "t1", result, expected_action="draft_reply", expected_domain="mail"
    )
    assert len(violations) == 1
    assert violations[0].assertion == "capability_selected"


def test_auto_apply_below_act_notify_is_flagged():
    """A forged/corrupted audit trail claiming auto_apply happened under a
    rung that should never allow it -- the invariant assert_autonomy_rung_respected
    exists to catch."""
    result = {
        "audit_events": [
            {"event": "autonomy_gate", "max_rung": int(Rung.PROPOSE), "routed_to": "auto_apply"},
        ],
    }
    violations = assert_autonomy_rung_respected("t2", result)
    assert len(violations) == 1
    assert violations[0].assertion == "autonomy_rung_respected"


def test_approve_route_decided_without_human_decision_event_is_flagged():
    result = {
        "decision": "approved",
        "audit_events": [
            {"event": "autonomy_gate", "max_rung": int(Rung.PROPOSE), "routed_to": "approve"},
        ],
    }
    violations = assert_autonomy_rung_respected("t3", result)
    assert len(violations) == 1


def test_write_on_approve_route_without_human_decision_is_flagged():
    result = {
        "applied_ref": "draft-xyz",
        "audit_events": [
            {"event": "autonomy_gate", "max_rung": int(Rung.PROPOSE), "routed_to": "approve"},
        ],
    }
    violations = assert_no_write_on_read_only("t4", result)
    assert len(violations) == 1
    assert violations[0].assertion == "no_write_on_read_only"


def test_honest_source_changed_report_has_no_violation():
    result = {
        "apply_error": "source_changed",
        "audit_events": [{"event": "apply_skipped", "reason": "source_changed"}],
    }
    assert assert_freshness_checked_before_apply("t5", result) == []


def test_silently_swallowed_source_changed_is_flagged():
    result = {"apply_error": "source_changed", "audit_events": []}
    violations = assert_freshness_checked_before_apply("t6", result)
    assert len(violations) == 1
    assert violations[0].assertion == "freshness_checked_before_apply"


def test_recording_memory_store_proves_min_score_is_threaded():
    pytest.importorskip("langgraph")
    recording = RecordingMemoryStore(_FakeStore())
    graph = build_draft_approve_graph(client=_FakeClient(), store=recording, min_score=0.5)
    cfg = {"configurable": {"thread_id": "t-floor"}}
    graph.invoke(_base_state(), cfg)

    violations = assert_retrieval_requested_score_floor("t-floor", recording, expected_min_score=0.5)
    assert violations == []
    assert recording.search_calls  # the assertion is not vacuous


def test_recording_memory_store_flags_a_missing_floor():
    pytest.importorskip("langgraph")
    recording = RecordingMemoryStore(_FakeStore())
    graph = build_draft_approve_graph(client=_FakeClient(), store=recording)  # no min_score passed
    cfg = {"configurable": {"thread_id": "t-no-floor"}}
    graph.invoke(_base_state(), cfg)

    violations = assert_retrieval_requested_score_floor("t-no-floor", recording, expected_min_score=0.5)
    assert len(violations) == 1
