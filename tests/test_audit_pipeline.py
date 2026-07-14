"""The end-to-end audit pipeline test (roadmap prompt 20, review finding #4).

This is the test the external review said was missing: prompt 12's suite
constructed audit entries by hand, certifying the track-record fold while
the production path (graph → dispatch-time record → resume-time record →
track_records) silently wrote nothing on resume. **Nothing in this file
constructs an audit entry directly** — every entry flows through the real
compiled graph and the real ``JsonlAuditLog``, exactly as production does.
"""

from __future__ import annotations

import pytest

from attune.audit.log import JsonlAuditLog
from attune.memory.base import MemoryStore
from attune.orchestrator import (
    Action,
    Domain,
    build_draft_approve_graph,
    resume_workflow,
    suggest_graduations,
    track_records,
)

pytest.importorskip("langgraph")


class _Store(MemoryStore):
    def add(self, *a, **kw):
        return []

    def search(self, *a, **kw):
        return []

    def get_all(self, *a, **kw):
        return []

    def delete(self, *a):
        pass


class _Client:
    def chat_completions_create(self, **kw):
        class _C:
            class message:
                content = "proposed draft"

        class _R:
            choices = [_C]
        return _R()


def _dispatch(graph, log, tid, *, domain="mail"):
    """Mirror the dispatcher's production behavior exactly: invoke, then
    record the pre-interrupt events against the workflow id."""
    result = graph.invoke(
        {
            "user_id": "u1", "domain": domain, "action": "draft_reply",
            "incoming_ref": "src-1", "incoming_summary": "hello",
            "audit_events": [], "iteration_count": 0,
        },
        {"configurable": {"thread_id": tid}},
    )
    log.record(
        thread_id=tid, workflow="draft_approve",
        events=result.get("audit_events", []),
        domain=domain, user_id="u1",
    )
    return result


def _pipeline(tmp_path, decisions, *, actor="U-OWNER"):
    """Run N full proposal→resume cycles through the production path and
    return the (real, file-backed) audit log."""
    log = JsonlAuditLog(str(tmp_path / "audit.jsonl"))
    graph = build_draft_approve_graph(
        client=_Client(), store=_Store(), apply_fn=lambda state: "draft-1"
    )
    for i, decision in enumerate(decisions):
        tid = f"gmail:t{i}:100"
        _dispatch(graph, log, tid)
        resume_workflow(
            graph, tid, decision,
            "my edit" if decision == "edited" else None,
            audit_log=log, user_id="u1", actor=actor,
        )
    return log


def test_track_records_sees_real_decisions(tmp_path):
    log = _pipeline(tmp_path, ["approved", "approved", "edited", "rejected"])

    records = track_records(log)
    record = records[(Action.DRAFT_REPLY, Domain.MAIL)]

    assert record.approved == 2
    assert record.edited == 1
    assert record.rejected == 1


def test_graduation_fires_on_real_approvals(tmp_path):
    """The M4 flagship, on production data: enough unedited approvals through
    the real pipeline produce a graduation suggestion."""
    from attune.orchestrator import default_matrix

    log = _pipeline(tmp_path, ["approved"] * 12)

    suggestions = suggest_graduations(log, default_matrix())

    assert len(suggestions) == 1
    assert "12/12" in suggestions[0].render()


def test_graduation_does_not_fire_when_real_apply_fails(tmp_path):
    log = JsonlAuditLog(str(tmp_path / "audit.jsonl"))
    graph = build_draft_approve_graph(
        client=_Client(),
        store=_Store(),
        apply_fn=lambda state: (_ for _ in ()).throw(RuntimeError("failed")),
    )
    for i in range(12):
        tid = f"gmail:t{i}:100"
        _dispatch(graph, log, tid)
        resume_workflow(graph, tid, "approved", audit_log=log, user_id="u1")

    from attune.orchestrator import default_matrix

    assert suggest_graduations(log, default_matrix()) == []
    record = track_records(log)[(Action.DRAFT_REPLY, Domain.MAIL)]
    assert (record.applied, record.apply_failed) == (0, 12)


def test_no_event_is_double_recorded(tmp_path):
    """The name-filter contract: dispatch records pre-interrupt events,
    resume records post-resume events — each exactly once."""
    log = _pipeline(tmp_path, ["approved"])

    entries = log.query(thread_id="gmail:t0:100")
    names = [e.event for e in entries]

    for name in ("retrieved", "drafted", "autonomy_gate",
                 "human_decision", "signal_captured"):
        assert names.count(name) == 1, f"{name} recorded {names.count(name)}x"


def test_actor_and_domain_stamped_on_resume(tmp_path):
    log = _pipeline(tmp_path, ["approved"], actor="U-ALICE")

    entries = log.query(thread_id="gmail:t0:100")
    decision = next(e for e in entries if e.event == "human_decision")

    assert decision.fields["actor"] == "U-ALICE"
    assert decision.domain == "mail"  # the workflow's domain, never "chat"


def test_calendar_resume_carries_calendar_domain(tmp_path):
    """A chat-channel click on a calendar card audits under 'calendar'."""
    log = JsonlAuditLog(str(tmp_path / "audit.jsonl"))
    graph = build_draft_approve_graph(client=_Client(), store=_Store())
    graph.invoke(
        {
            "user_id": "u1", "domain": "calendar", "action": "create_hold",
            "incoming_ref": "e1", "incoming_summary": "conflict",
            "hold_start": "2026-07-10T14:00:00+00:00",
            "hold_end": "2026-07-10T14:30:00+00:00",
            "audit_events": [], "iteration_count": 0,
        },
        {"configurable": {"thread_id": "cal-1"}},
    )
    resume_workflow(graph, "cal-1", "approved", audit_log=log, user_id="u1")

    decision = next(
        e for e in log.query(thread_id="cal-1") if e.event == "human_decision"
    )
    assert decision.domain == "calendar"


def test_audit_failure_never_breaks_resume(tmp_path):
    class _ExplodingLog:
        def record(self, **kw):
            raise RuntimeError("disk full")

    graph = build_draft_approve_graph(client=_Client(), store=_Store())
    graph.invoke(
        {
            "user_id": "u1", "domain": "mail", "action": "draft_reply",
            "incoming_ref": "r1", "incoming_summary": "hi",
            "audit_events": [], "iteration_count": 0,
        },
        {"configurable": {"thread_id": "t-explode"}},
    )
    out = resume_workflow(
        graph, "t-explode", "approved", audit_log=_ExplodingLog(), user_id="u1"
    )
    assert out["decision"] == "approved"  # the resume itself succeeded
