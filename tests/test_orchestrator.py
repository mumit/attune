"""Tests for the draft-and-approve orchestrator graph.

A fake configured OpenAI-compatible gateway client and a fake MemoryStore keep these tests free of any live
model or vector store, while exercising the real graph: retrieval, drafting, the
autonomy gate's routing, the human-approval interrupt, resume, and signal
capture.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from attune.memory.base import MemoryRecord, MemoryStore
from attune.memory.signals import ActionSignal
from attune.orchestrator import (
    Action,
    Domain,
    Rung,
    apply_confirmation,
    build_draft_approve_graph,
    default_matrix,
    make_connector_apply_fn,
    resume_workflow,
)

langgraph = pytest.importorskip("langgraph")
from langgraph.types import Command  # noqa: E402


class FakeStore(MemoryStore):
    def __init__(self):
        self.added: list[dict] = []

    def add(self, messages, *, user_id, metadata=None, infer=True):
        self.added.append({"metadata": metadata, "infer": infer})
        return []

    def search(self, query, *, user_id, limit=8, min_score=None):
        return [MemoryRecord(id="1", text="prefers short replies", score=0.9)]

    def get_all(self, *, user_id, limit=100):
        return []

    def delete(self, memory_id):
        pass


class FakeMsg:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})


class FakeResp:
    def __init__(self, content):
        self.choices = [FakeMsg(content)]


class FakeClient:
    def __init__(self):
        self.calls = []

    def chat_completions_create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResp("Hi — short reply as you prefer.")


def _base_state(**over):
    s = {
        "user_id": "mumit",
        "domain": "mail",
        "action": "draft_reply",
        "incoming_ref": "msg-123",
        "incoming_summary": "Vendor asking to reschedule Thursday's call.",
        "audit_events": [],
        "iteration_count": 0,
    }
    s.update(over)
    return s


CFG = {"configurable": {"thread_id": "t1"}}


def test_pauses_at_approval_by_default():
    store = FakeStore()
    graph = build_draft_approve_graph(client=FakeClient(), store=store)
    result = graph.invoke(_base_state(), CFG)
    # default posture: no autonomous send grant -> must interrupt for approval
    assert "__interrupt__" in result
    payload = result["__interrupt__"][0].value
    assert payload["question"] == "Approve this draft?"
    assert payload["proposed_draft"]


def test_approve_path_sets_final_and_captures_signal():
    store = FakeStore()
    graph = build_draft_approve_graph(client=FakeClient(), store=store)
    graph.invoke(_base_state(), CFG)
    out = graph.invoke(Command(resume={"decision": "approved"}), CFG)
    assert out["decision"] == "approved"
    assert out["final_text"]
    # an action signal was captured verbatim
    assert any(a["metadata"]["signal"] == "action" for a in store.added)


def test_edit_path_captures_correction():
    store = FakeStore()
    graph = build_draft_approve_graph(client=FakeClient(), store=store)
    cfg = {"configurable": {"thread_id": "t-edit"}}
    graph.invoke(_base_state(), cfg)
    out = graph.invoke(
        Command(resume={"decision": "edited", "text": "Sure, Thursday works."}), cfg
    )
    assert out["final_text"] == "Sure, Thursday works."
    # both a correction and an action signal recorded
    signals = [a["metadata"]["signal"] for a in store.added]
    assert "correction" in signals and "action" in signals


def test_reject_path_no_final_text():
    store = FakeStore()
    graph = build_draft_approve_graph(client=FakeClient(), store=store)
    cfg = {"configurable": {"thread_id": "t-rej"}}
    graph.invoke(_base_state(), cfg)
    out = graph.invoke(Command(resume={"decision": "rejected"}), cfg)
    assert out["decision"] == "rejected"
    assert out.get("final_text") is None


# ---------------------------------------------------------------------------
# LABEL captures (Phase 3 stage 1, docs/future-state.md; G9) — the hygiene-
# action approval-signal asymmetry, and the deterministic archive draft_fn.
# ---------------------------------------------------------------------------


class _FakeImportanceProfileForCapture:
    """Records every record_signal call, for the dual-write pin test."""

    def __init__(self):
        self.calls: list[tuple] = []

    def record_signal(self, sender, signal, *, ts=None):
        self.calls.append((sender, signal))


def test_label_capture_does_not_dual_write_importance_profile():
    """Deliverable B's pin: approving an archive proposal must NOT be
    recorded as positive engagement for the sender in the importance
    profile — that would push a noisy sender's tier toward HIGH, exactly
    backwards. The raw signal still reaches memory, tagged hygiene_action,
    for nightly consolidation."""
    store = FakeStore()
    profile = _FakeImportanceProfileForCapture()
    graph = build_draft_approve_graph(
        client=FakeClient(), store=store, importance_profile=profile,
    )
    cfg = {"configurable": {"thread_id": "t-label-capture"}}
    graph.invoke(_base_state(action="label", sender="noisy@x.com"), cfg)
    graph.invoke(Command(resume={"decision": "approved"}), cfg)

    assert profile.calls == []  # the sender's profile is untouched

    action_entries = [
        a for a in store.added if a["metadata"]["signal"] == "action"
    ]
    assert len(action_entries) == 1
    assert action_entries[0]["metadata"].get("hygiene_action") is True


def test_non_label_capture_still_dual_writes_importance_profile():
    """Contrast case: a normal DRAFT_REPLY approval still feeds the
    importance profile exactly as Phase 1 established — the asymmetry is
    specific to LABEL, not a regression for every other action."""
    store = FakeStore()
    profile = _FakeImportanceProfileForCapture()
    graph = build_draft_approve_graph(
        client=FakeClient(), store=store, importance_profile=profile,
    )
    cfg = {"configurable": {"thread_id": "t-reply-capture"}}
    graph.invoke(_base_state(sender="vendor@acme.com"), cfg)
    graph.invoke(Command(resume={"decision": "approved"}), cfg)

    assert profile.calls == [("vendor@acme.com", ActionSignal.APPROVED)]


def test_archive_draft_fn_is_deterministic_no_model_call():
    """The archive proposal's draft_fn just echoes incoming_summary back —
    no model call, since the thread was already classified by triage."""
    from attune.orchestrator.draft_approve import archive_draft_fn

    text = "Archive 'Sale!' from deals@x.com — triaged noise: newsletter"
    assert archive_draft_fn(None, text, ["irrelevant memory"], "mail") == text


def test_label_graph_never_calls_the_model():
    """End-to-end: a graph compiled with archive_draft_fn never touches the
    injected client, even through the normal retrieve -> draft flow."""
    from attune.orchestrator.draft_approve import archive_draft_fn

    store = FakeStore()
    client = FakeClient()
    graph = build_draft_approve_graph(
        client=client, store=store, draft_fn=archive_draft_fn,
    )
    text = "Archive 'Sale!' from deals@x.com — triaged noise: newsletter"
    out = graph.invoke(
        _base_state(action="label", incoming_summary=text),
        {"configurable": {"thread_id": "t-label-draft"}},
    )
    assert out["proposed_draft"] == text
    assert client.calls == []


# ---------------------------------------------------------------------------
# DECLINE_INVITE/RESCHEDULE captures (Phase 3 stage 2) — the generalized
# HYGIENE_ACTIONS capture rule, and the deterministic calendar_action draft_fn.
# ---------------------------------------------------------------------------


def test_decline_invite_capture_does_not_dual_write_importance_profile():
    """Deliverable C's pin: approving a decline-invite proposal must NOT be
    recorded as positive engagement for the organizer -- same asymmetry as
    LABEL, generalized via HYGIENE_ACTIONS."""
    store = FakeStore()
    profile = _FakeImportanceProfileForCapture()
    graph = build_draft_approve_graph(
        client=FakeClient(), store=store, importance_profile=profile,
    )
    cfg = {"configurable": {"thread_id": "t-decline-capture"}}
    graph.invoke(
        _base_state(action="decline_invite", domain="calendar", sender=None), cfg
    )
    graph.invoke(Command(resume={"decision": "approved"}), cfg)

    assert profile.calls == []  # the organizer's profile is untouched
    action_entries = [a for a in store.added if a["metadata"]["signal"] == "action"]
    assert len(action_entries) == 1
    assert action_entries[0]["metadata"].get("hygiene_action") is True


def test_reschedule_capture_does_not_dual_write_importance_profile():
    """Same pin, for RESCHEDULE: approving a reschedule proposal leaves the
    organizer's profile unchanged."""
    store = FakeStore()
    profile = _FakeImportanceProfileForCapture()
    graph = build_draft_approve_graph(
        client=FakeClient(), store=store, importance_profile=profile,
    )
    cfg = {"configurable": {"thread_id": "t-reschedule-capture"}}
    graph.invoke(
        _base_state(action="reschedule", domain="calendar", sender=None), cfg
    )
    graph.invoke(Command(resume={"decision": "approved"}), cfg)

    assert profile.calls == []
    action_entries = [a for a in store.added if a["metadata"]["signal"] == "action"]
    assert len(action_entries) == 1
    assert action_entries[0]["metadata"].get("hygiene_action") is True


def test_hygiene_actions_set_contains_all_three_stage_actions():
    from attune.orchestrator.autonomy import Action
    from attune.orchestrator.draft_approve import HYGIENE_ACTIONS

    assert HYGIENE_ACTIONS == {
        Action.LABEL.value, Action.DECLINE_INVITE.value, Action.RESCHEDULE.value,
    }


def test_calendar_action_draft_fn_is_deterministic_no_model_call():
    """Mirrors archive_draft_fn: the dispatcher already computed the
    deterministic reason/slot text, so there is nothing left to draft."""
    from attune.orchestrator.draft_approve import calendar_action_draft_fn

    text = "Decline 'Q3 sync' — conflicts with 'Design review'"
    assert calendar_action_draft_fn(None, text, ["irrelevant memory"], "calendar") == text


def test_calendar_action_graph_never_calls_the_model():
    from attune.orchestrator.draft_approve import calendar_action_draft_fn

    store = FakeStore()
    client = FakeClient()
    graph = build_draft_approve_graph(
        client=client, store=store, draft_fn=calendar_action_draft_fn,
    )
    text = "Move 'Standup' (30 min) to Tue 15:00–15:30 — conflicts with 'Sync'"
    out = graph.invoke(
        _base_state(action="reschedule", domain="calendar", incoming_summary=text),
        {"configurable": {"thread_id": "t-calendar-action-draft"}},
    )
    assert out["proposed_draft"] == text
    assert client.calls == []


def test_autonomy_grant_skips_interrupt():
    store = FakeStore()
    # grant autonomous send on (draft_reply, mail) at ACT_NOTIFY
    matrix = default_matrix().grant(Action.DRAFT_REPLY, Domain.MAIL, Rung.ACT_NOTIFY)
    graph = build_draft_approve_graph(client=FakeClient(), store=store, matrix=matrix)
    cfg = {"configurable": {"thread_id": "t-auto"}}
    out = graph.invoke(_base_state(), cfg)
    # no interrupt: went straight through auto_apply to completion
    assert "__interrupt__" not in out
    assert out["decision"] == "approved"
    assert out["final_text"]


def test_untrusted_content_marked_in_prompt():
    store = FakeStore()
    client = FakeClient()
    graph = build_draft_approve_graph(client=client, store=store)
    graph.invoke(_base_state(), {"configurable": {"thread_id": "t-prompt"}})
    # the drafting call tagged the incoming content as untrusted
    user_msg = client.calls[0]["messages"][-1]["content"]
    assert "UNTRUSTED" in user_msg


def test_audit_trail_accumulates():
    store = FakeStore()
    graph = build_draft_approve_graph(client=FakeClient(), store=store)
    cfg = {"configurable": {"thread_id": "t-audit"}}
    graph.invoke(_base_state(), cfg)
    out = graph.invoke(Command(resume={"decision": "approved"}), cfg)
    events = [e["event"] for e in out["audit_events"]]
    # the full reason-for-action chain is present and ordered
    assert events[:3] == ["retrieved", "drafted", "autonomy_gate"]
    assert "human_decision" in events and "signal_captured" in events


# ---------------------------------------------------------------------------
# apply — materializing the decision (prompt 01: Approve must produce a real
# Gmail draft via the injected apply_fn, never a dead end)
# ---------------------------------------------------------------------------


def test_apply_called_with_final_text_on_approval():
    store = FakeStore()
    applied: list[dict] = []
    graph = build_draft_approve_graph(
        client=FakeClient(),
        store=store,
        apply_fn=lambda state: applied.append(state) or "draft-abc",
    )
    cfg = {"configurable": {"thread_id": "t-apply"}}
    graph.invoke(_base_state(), cfg)
    out = graph.invoke(Command(resume={"decision": "approved"}), cfg)

    assert len(applied) == 1
    assert applied[0]["final_text"] == out["final_text"]
    assert applied[0]["incoming_ref"] == "msg-123"
    assert out["applied_ref"] == "draft-abc"
    events = [e["event"] for e in out["audit_events"]]
    assert "applied" in events


def test_apply_called_on_edited_with_edited_text():
    store = FakeStore()
    applied: list[dict] = []
    graph = build_draft_approve_graph(
        client=FakeClient(),
        store=store,
        apply_fn=lambda state: applied.append(state) or "draft-edit",
    )
    cfg = {"configurable": {"thread_id": "t-apply-edit"}}
    graph.invoke(_base_state(), cfg)
    out = graph.invoke(
        Command(resume={"decision": "edited", "text": "Custom reply."}), cfg
    )

    assert applied[0]["final_text"] == "Custom reply."
    assert out["applied_ref"] == "draft-edit"


def test_apply_skipped_on_rejection():
    store = FakeStore()
    applied: list[dict] = []
    graph = build_draft_approve_graph(
        client=FakeClient(),
        store=store,
        apply_fn=lambda state: applied.append(state) or "never",
    )
    cfg = {"configurable": {"thread_id": "t-apply-rej"}}
    graph.invoke(_base_state(), cfg)
    out = graph.invoke(Command(resume={"decision": "rejected"}), cfg)

    assert applied == []
    assert out.get("applied_ref") is None
    events = [e["event"] for e in out["audit_events"]]
    assert "apply_skipped" in events and "applied" not in events


def test_apply_runs_on_autonomous_path_too():
    store = FakeStore()
    applied: list[dict] = []
    matrix = default_matrix().grant(Action.DRAFT_REPLY, Domain.MAIL, Rung.ACT_NOTIFY)
    graph = build_draft_approve_graph(
        client=FakeClient(),
        store=store,
        matrix=matrix,
        apply_fn=lambda state: applied.append(state) or "draft-auto",
    )
    out = graph.invoke(_base_state(), {"configurable": {"thread_id": "t-apply-auto"}})

    assert len(applied) == 1
    assert out["applied_ref"] == "draft-auto"


def test_apply_failure_recorded_not_raised():
    """An apply failure must not lose the decision or the capture step, and
    must be visible in state + audit so the channel can report it honestly."""
    store = FakeStore()

    def boom(state):
        raise ConnectionError("gmail down")

    graph = build_draft_approve_graph(client=FakeClient(), store=store, apply_fn=boom)
    cfg = {"configurable": {"thread_id": "t-apply-fail"}}
    graph.invoke(_base_state(), cfg)
    out = graph.invoke(Command(resume={"decision": "approved"}), cfg)

    assert out["decision"] == "approved"
    assert out.get("applied_ref") is None
    assert out["apply_error"] == "ConnectionError"
    events = [e["event"] for e in out["audit_events"]]
    assert "apply_failed" in events
    # capture still ran: the decision signal was not lost
    assert "signal_captured" in events
    assert any(a["metadata"]["signal"] == "action" for a in store.added)


def test_default_apply_is_noop():
    store = FakeStore()
    graph = build_draft_approve_graph(client=FakeClient(), store=store)
    cfg = {"configurable": {"thread_id": "t-apply-default"}}
    graph.invoke(_base_state(), cfg)
    out = graph.invoke(Command(resume={"decision": "approved"}), cfg)

    assert out.get("applied_ref") is None
    assert out.get("apply_error") is None
    events = [e["event"] for e in out["audit_events"]]
    assert "apply_skipped" in events


# ---------------------------------------------------------------------------
# make_connector_apply_fn — the production apply: create_draft, never send
# ---------------------------------------------------------------------------


class _FakeDraftRef:
    def __init__(self, draft_id):
        self.draft_id = draft_id


class _FakeConnector:
    def __init__(self):
        self.drafts: list[dict] = []

    def get_thread(self, thread_id):
        return type(
            "T", (), {"subject": "Quarterly sync", "from_addr": "vendor@example.com"}
        )()

    def create_draft(self, *, to, subject, body, thread_id=None):
        self.drafts.append(
            {"to": to, "subject": subject, "body": body, "thread_id": thread_id}
        )
        return _FakeDraftRef("d-99")


def test_connector_apply_fn_creates_reply_draft():
    conn = _FakeConnector()
    apply = make_connector_apply_fn(conn)
    ref = apply(
        {
            "domain": "mail",
            "incoming_ref": "thr-1",
            "final_text": "Sounds good — Thursday works.",
        }
    )

    assert ref == "d-99"
    d = conn.drafts[0]
    assert d["to"] == "vendor@example.com"
    assert d["subject"] == "Re: Quarterly sync"
    assert d["body"] == "Sounds good — Thursday works."
    assert d["thread_id"] == "thr-1"


def test_connector_apply_fn_preserves_existing_re_prefix():
    conn = _FakeConnector()
    conn.get_thread = lambda tid: type(
        "T", (), {"subject": "RE: Quarterly sync", "from_addr": "v@example.com"}
    )()
    apply = make_connector_apply_fn(conn)
    apply({"domain": "mail", "incoming_ref": "thr-1", "final_text": "ok"})

    assert conn.drafts[0]["subject"] == "RE: Quarterly sync"


def test_apply_targets_reply_to_over_first_sender():
    """Prompt 18: the recipient is the newest counterparty (reply_to), not
    the thread's first sender — the M5 follow-up case where they differ."""
    conn = _FakeConnector()
    conn.get_thread = lambda tid: type(
        "T", (), {"subject": "Redline", "from_addr": "me@example.com",
                  "last_from_addr": "me@example.com",
                  "reply_to": "marcus@acme.com"}
    )()
    apply = make_connector_apply_fn(conn, owner_email="me@example.com")
    ref = apply({"domain": "mail", "incoming_ref": "t1", "final_text": "ping"})

    assert ref == "d-99"
    assert conn.drafts[0]["to"] == "marcus@acme.com"


def test_apply_refuses_to_draft_to_the_owner():
    """An owner-only thread resolves the recipient to the owner: apply must
    refuse — the assistant never drafts to its own principal."""
    conn = _FakeConnector()
    conn.get_thread = lambda tid: type(
        "T", (), {"subject": "Note to self", "from_addr": "Me <me@example.com>",
                  "last_from_addr": "Me <me@example.com>", "reply_to": ""}
    )()
    apply = make_connector_apply_fn(conn, owner_email="me@example.com")
    ref = apply({"domain": "mail", "incoming_ref": "t1", "final_text": "hello me"})

    assert ref is None
    assert conn.drafts == []


def test_apply_refuses_empty_recipient():
    conn = _FakeConnector()
    conn.get_thread = lambda tid: type(
        "T", (), {"subject": "", "from_addr": "", "last_from_addr": "",
                  "reply_to": ""}
    )()
    apply = make_connector_apply_fn(conn)
    assert apply({"domain": "mail", "incoming_ref": "t1", "final_text": "x"}) is None
    assert conn.drafts == []


def test_connector_apply_fn_noop_outside_mail_or_without_ref():
    conn = _FakeConnector()
    apply = make_connector_apply_fn(conn)

    assert apply({"domain": "chat", "incoming_ref": "x", "final_text": "hi"}) is None
    assert apply({"domain": "mail", "final_text": "hi"}) is None
    assert apply({"domain": "mail", "incoming_ref": "x"}) is None
    assert conn.drafts == []


# ---------------------------------------------------------------------------
# Calendar hold apply (prompt 16): approval materializes the exact slot
# ---------------------------------------------------------------------------


def test_calendar_hold_end_to_end_conflict_card_approve_hold():
    """Conflict → CREATE_HOLD workflow → interrupt (PROPOSE gate) → approve →
    create_hold called with exactly the slot the card showed."""

    class _HoldConnector(_FakeConnector):
        def __init__(self):
            self.holds: list = []

        def create_hold(self, event):
            self.holds.append(event)
            return "hold-123"

    conn = _HoldConnector()
    graph = build_draft_approve_graph(
        client=FakeClient(), store=FakeStore(),
        apply_fn=make_connector_apply_fn(conn),
    )
    cfg = {"configurable": {"thread_id": "t-hold"}}
    paused = graph.invoke(
        {
            "user_id": "u1", "domain": "calendar", "action": "create_hold",
            "incoming_ref": "e1",
            "incoming_summary": "two meetings collided; propose the 14:00 slot",
            "hold_start": "2026-07-10T14:00:00+00:00",
            "hold_end": "2026-07-10T14:30:00+00:00",
            "hold_summary": "HOLD: Client call",
            "audit_events": [], "iteration_count": 0,
        },
        cfg,
    )
    assert "__interrupt__" in paused  # CREATE_HOLD is PROPOSE by default

    out = graph.invoke(Command(resume={"decision": "approved"}), cfg)

    assert out["applied_ref"] == "hold-123"
    hold = conn.holds[0]
    assert hold.summary == "HOLD: Client call"
    assert hold.start.isoformat() == "2026-07-10T14:00:00+00:00"
    assert hold.attendees == []  # never invites anyone (decisions entry)
    assert apply_confirmation("approved", out) == (
        "✅ Approved — tentative hold created on your calendar."
    )


def test_calendar_hold_rejected_creates_nothing():
    class _HoldConnector(_FakeConnector):
        def __init__(self):
            self.holds: list = []

        def create_hold(self, event):
            self.holds.append(event)
            return "hold-x"

    conn = _HoldConnector()
    graph = build_draft_approve_graph(
        client=FakeClient(), store=FakeStore(),
        apply_fn=make_connector_apply_fn(conn),
    )
    cfg = {"configurable": {"thread_id": "t-hold-rej"}}
    graph.invoke(
        {
            "user_id": "u1", "domain": "calendar", "action": "create_hold",
            "incoming_ref": "e1", "incoming_summary": "conflict",
            "hold_start": "2026-07-10T14:00:00+00:00",
            "hold_end": "2026-07-10T14:30:00+00:00",
            "audit_events": [], "iteration_count": 0,
        },
        cfg,
    )
    graph.invoke(Command(resume={"decision": "rejected"}), cfg)

    assert conn.holds == []


def test_calendar_apply_without_slot_is_noop():
    conn = _FakeConnector()
    apply = make_connector_apply_fn(conn)
    assert apply({"domain": "calendar", "final_text": "some prose"}) is None


# ---------------------------------------------------------------------------
# Freshness at apply (prompt 21): stale cards must not act on changed sources
# ---------------------------------------------------------------------------

from datetime import datetime as _dt, timezone as _tz  # noqa: E402


def test_stale_mail_apply_refused_with_source_changed():
    """The thread gained a message after the card was posted: apply
    refuses, the confirmation says so, nothing is created."""

    class _Conn(_FakeConnector):
        def get_thread(self, tid):
            return type("T", (), {
                "subject": "Redline", "from_addr": "a@x.com",
                "last_from_addr": "a@x.com", "reply_to": "a@x.com",
                "last_message_at": _dt(2026, 7, 10, 12, 0, tzinfo=_tz.utc),
            })()

    conn = _Conn()
    graph = build_draft_approve_graph(
        client=FakeClient(), store=FakeStore(),
        apply_fn=make_connector_apply_fn(conn),
    )
    cfg = {"configurable": {"thread_id": "t-stale"}}
    graph.invoke(_base_state(
        source_snapshot="2026-07-10T09:00:00+00:00",  # older than the thread
    ), cfg)
    out = graph.invoke(Command(resume={"decision": "approved"}), cfg)

    assert out.get("applied_ref") is None
    assert out["apply_error"] == "source_changed"
    assert conn.drafts == []
    text = apply_confirmation("approved", out)
    assert "changed since this card was posted" in text
    assert "re-review" in text.lower()


def test_fresh_mail_apply_proceeds():
    class _Conn(_FakeConnector):
        def get_thread(self, tid):
            return type("T", (), {
                "subject": "Redline", "from_addr": "a@x.com",
                "last_from_addr": "a@x.com", "reply_to": "a@x.com",
                "last_message_at": _dt(2026, 7, 10, 9, 0, tzinfo=_tz.utc),
            })()

    conn = _Conn()
    graph = build_draft_approve_graph(
        client=FakeClient(), store=FakeStore(),
        apply_fn=make_connector_apply_fn(conn),
    )
    cfg = {"configurable": {"thread_id": "t-fresh"}}
    graph.invoke(_base_state(
        source_snapshot="2026-07-10T09:00:00+00:00",  # unchanged
    ), cfg)
    out = graph.invoke(Command(resume={"decision": "approved"}), cfg)

    assert out["applied_ref"] == "d-99"


def test_no_snapshot_proceeds_for_backcompat():
    conn = _FakeConnector()
    graph = build_draft_approve_graph(
        client=FakeClient(), store=FakeStore(),
        apply_fn=make_connector_apply_fn(conn),
    )
    cfg = {"configurable": {"thread_id": "t-nosnap"}}
    graph.invoke(_base_state(), cfg)  # no source_snapshot in state
    out = graph.invoke(Command(resume={"decision": "approved"}), cfg)
    assert out["applied_ref"] == "d-99"


def test_moved_event_hold_apply_refused():
    """The conflicted meeting was rescheduled after the card was posted:
    the hold must not be created against a resolved conflict."""

    class _Conn(_FakeConnector):
        def __init__(self):
            self.holds = []

        def get_event(self, event_id):
            return type("E", (), {
                "start": _dt(2026, 7, 10, 15, 0, tzinfo=_tz.utc),
            })()

        def create_hold(self, event):
            self.holds.append(event)
            return "hold-x"

    conn = _Conn()
    graph = build_draft_approve_graph(
        client=FakeClient(), store=FakeStore(),
        apply_fn=make_connector_apply_fn(conn),
    )
    cfg = {"configurable": {"thread_id": "t-hold-stale"}}
    graph.invoke(
        {
            "user_id": "u1", "domain": "calendar", "action": "create_hold",
            "incoming_ref": "e1", "incoming_summary": "conflict",
            "hold_start": "2026-07-10T14:00:00+00:00",
            "hold_end": "2026-07-10T14:30:00+00:00",
            "source_snapshot": "2026-07-10T10:00:00+00:00",  # event has moved
            "audit_events": [], "iteration_count": 0,
        },
        cfg,
    )
    out = graph.invoke(Command(resume={"decision": "approved"}), cfg)

    assert out["apply_error"] == "source_changed"
    assert conn.holds == []
    text = apply_confirmation("approved", out)
    assert "meeting changed" in text


# ---------------------------------------------------------------------------
# apply_confirmation — honest channel text (rule 4: never claim "sending")
# ---------------------------------------------------------------------------


def test_confirmation_reports_created_draft():
    text = apply_confirmation("approved", {"applied_ref": "d-1"})
    assert text == "✅ Approved — draft created in Gmail."


def test_confirmation_plain_when_nothing_materialized():
    assert apply_confirmation("approved", {"applied_ref": None}) == "✅ Approved."
    # a fake/None resume result (e.g. injected resume_fn in tests) is tolerated
    assert apply_confirmation("approved", None) == "✅ Approved."


def test_confirmation_admits_apply_failure():
    text = apply_confirmation("approved", {"apply_error": "ConnectionError"})
    assert "failed" in text and "ConnectionError" in text
    assert "recorded" in text


def test_confirmation_edited_and_rejected():
    assert apply_confirmation("edited", {"applied_ref": "d-2"}) == (
        "✏️ Edited — draft created in Gmail."
    )
    assert apply_confirmation("rejected", {}) == "🗑️ Rejected — nothing sent."


def test_confirmation_never_says_sending():
    for decision in ("approved", "edited", "rejected"):
        for result in (None, {}, {"applied_ref": "d"}, {"apply_error": "E"}):
            assert "sending" not in apply_confirmation(decision, result).lower()


# ---------------------------------------------------------------------------
# resume_workflow — the shared Command(resume=...) invoke (design decision:
# used by SlackChannel/GoogleChatChannel's default resume_fn, and by
# dispatcher.handle_chat_interaction's async Chat-interaction path)
# ---------------------------------------------------------------------------


def test_resume_workflow_approves():
    store = FakeStore()
    graph = build_draft_approve_graph(client=FakeClient(), store=store)
    cfg = {"configurable": {"thread_id": "t-resume-approve"}}
    graph.invoke(_base_state(), cfg)

    out = resume_workflow(graph, "t-resume-approve", "approved")

    assert out["decision"] == "approved"
    assert out["final_text"]


def test_resume_workflow_rejects():
    store = FakeStore()
    graph = build_draft_approve_graph(client=FakeClient(), store=store)
    cfg = {"configurable": {"thread_id": "t-resume-reject"}}
    graph.invoke(_base_state(), cfg)

    out = resume_workflow(graph, "t-resume-reject", "rejected")

    assert out["decision"] == "rejected"
    assert out.get("final_text") is None


def test_resume_workflow_edits_with_text():
    store = FakeStore()
    graph = build_draft_approve_graph(client=FakeClient(), store=store)
    cfg = {"configurable": {"thread_id": "t-resume-edit"}}
    graph.invoke(_base_state(), cfg)

    out = resume_workflow(graph, "t-resume-edit", "edited", "Sure, works for me.")

    assert out["final_text"] == "Sure, works for me."


def test_resume_workflow_resolves_pending_entry():
    """resume_workflow is the single resume path, so it's the one place every
    decision marks its pending card resolved (prompt 03)."""

    class _FakePending:
        def __init__(self):
            self.resolved = []

        def resolve(self, lg_tid):
            self.resolved.append(lg_tid)

    store = FakeStore()
    graph = build_draft_approve_graph(client=FakeClient(), store=store)
    cfg = {"configurable": {"thread_id": "t-resume-pending"}}
    graph.invoke(_base_state(), cfg)

    pending = _FakePending()
    resume_workflow(graph, "t-resume-pending", "approved", pending=pending)

    assert pending.resolved == ["t-resume-pending"]


def test_resume_workflow_omits_text_key_when_none():
    """No text -> no 'text' key in the resume payload at all (not text=None),
    since the approve node reads state.get('proposed_draft') as the fallback
    when 'text' is absent."""
    store = FakeStore()
    graph = build_draft_approve_graph(client=FakeClient(), store=store)
    cfg = {"configurable": {"thread_id": "t-resume-notext"}}
    result = graph.invoke(_base_state(), cfg)
    proposed = result["__interrupt__"][0].value["proposed_draft"]

    out = resume_workflow(graph, "t-resume-notext", "approved", None)

    assert out["final_text"] == proposed


def test_resume_workflow_does_not_invoke_twice_after_claim(tmp_path):
    from attune.orchestrator import JsonPendingApprovals

    store = FakeStore()
    graph = build_draft_approve_graph(client=FakeClient(), store=store)
    cfg = {"configurable": {"thread_id": "t-single-use"}}
    graph.invoke(_base_state(), cfg)
    pending = JsonPendingApprovals(str(tmp_path / "pending.json"))
    pending.register(
        lg_tid="t-single-use", source_ref="src", domain="mail",
        posted_at=datetime.now(timezone.utc),
    )

    first = resume_workflow(
        graph, "t-single-use", "approved", pending=pending, actor="U1"
    )
    second = resume_workflow(
        graph, "t-single-use", "approved", pending=pending, actor="U1"
    )

    assert first["decision"] == "approved"
    assert second["approval_already_handled"] is True
