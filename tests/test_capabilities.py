"""Tests for the capability registry (build prompt 30).

Covers the acceptance criteria specific to the registry design itself:
- one compiled graph handles every registered capability, and a resume can
  never run another capability's apply function;
- a brand-new capability can be added entirely within a test, touching no
  production file outside the registry;
- the three independent gates (matrix rung, connector probe, deployment
  flag) refuse independently for a new capability (mark_read, RSVP_ACCEPT).

test_runtime.py separately covers the resume-routing regression pin at the
runtime.py layer; this file covers the registry/graph layer directly.
"""

from __future__ import annotations

import pytest

from attune.memory.base import MemoryRecord, MemoryStore
from attune.orchestrator import (
    Action,
    Domain,
    PermissionMatrix,
    RiskTier,
    Rung,
    build_capability_registry,
    build_draft_approve_graph,
    default_matrix,
)
from attune.orchestrator.capabilities import Capability, CapabilityRegistry, capability_gates_pass

langgraph = pytest.importorskip("langgraph")
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402


class FakeStore(MemoryStore):
    def add(self, messages, *, user_id, metadata=None, infer=True):
        return []

    def search(self, query, *, user_id, limit=8, min_score=None):
        return []

    def get_all(self, *, user_id, limit=100):
        return []

    def delete(self, memory_id):
        pass


class FakeClient:
    def chat_completions_create(self, **kwargs):
        class _Msg:
            content = "a drafted reply"

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        return _Resp()


class _FullMatrixConnector:
    """A connector that structurally supports every gated operation this
    registry's capabilities probe for."""

    def supports_sending(self):
        return True

    def supports_labeling(self):
        return True

    def supports_add_label(self):
        return True

    def supports_calendar_writes(self):
        return True


def _granted_matrix() -> PermissionMatrix:
    """Every registered action granted PROPOSE on its domain — enough for
    every capability's gate to route to human approval (never auto-apply,
    since PROPOSE < ACT_NOTIFY)."""
    m = default_matrix()
    for action, domain in (
        (Action.LABEL, Domain.MAIL),
        (Action.ADD_LABEL, Domain.MAIL),
        (Action.REMOVE_LABEL, Domain.MAIL),
        (Action.MARK_READ, Domain.MAIL),
        (Action.SEND_REPLY, Domain.MAIL),
        (Action.DECLINE_INVITE, Domain.CALENDAR),
        (Action.RESCHEDULE, Domain.CALENDAR),
        (Action.RSVP_ACCEPT, Domain.CALENDAR),
        (Action.RSVP_TENTATIVE, Domain.CALENDAR),
    ):
        m = m.grant(action, domain, Rung.PROPOSE)
    return m


# ---------------------------------------------------------------------------
# One compiled graph handles every registered capability; a resume can
# never run another capability's apply function.
# ---------------------------------------------------------------------------


def test_one_graph_handles_every_registered_capability():
    """Drives every one of the registry's 12 capabilities through the SAME
    compiled graph instance and asserts each one's OWN apply function fired
    — proof that one graph genuinely serves every capability, not just the
    six pre-existing ones."""
    applied: dict[str, list] = {}

    def _make_apply(name):
        def apply(state):
            applied.setdefault(name, []).append(state)
            return f"{name}-ref"

        return apply

    registry = build_capability_registry(
        apply_fn=_make_apply("shared"),
        label_apply_fn=_make_apply("label"),
        calendar_action_apply_fn=_make_apply("calendar_action"),
        add_label_apply_fn=_make_apply("add_label"),
        remove_label_apply_fn=_make_apply("remove_label"),
        mark_read_apply_fn=_make_apply("mark_read"),
        accept_invite_apply_fn=_make_apply("accept_invite"),
        tentative_invite_apply_fn=_make_apply("tentative_invite"),
    )
    assert len(registry) == 12

    graph = build_draft_approve_graph(
        client=FakeClient(), store=FakeStore(), checkpointer=InMemorySaver(),
        registry=registry,
    )

    for i, action in enumerate(registry):
        thread_id = f"t-{i}"
        cfg = {"configurable": {"thread_id": thread_id}}
        graph.invoke(
            {
                "user_id": "u1", "domain": action.domain.value,
                "action": action.action.value, "incoming_ref": f"ref-{i}",
                "incoming_summary": "test", "label_name": "Finance",
                "audit_events": [],
            },
            cfg,
        )
        graph.invoke(Command(resume={"decision": "approved"}), cfg)

    # Every capability that shares "shared"/"label"/"calendar_action" fired
    # exactly the actions registered against it, never a sibling's.
    assert len(applied.get("shared", [])) == 4  # draft_reply, send_reply, create_hold, follow_up
    assert len(applied.get("label", [])) == 1  # label
    assert len(applied.get("calendar_action", [])) == 2  # decline_invite, reschedule
    assert len(applied.get("add_label", [])) == 1
    assert len(applied.get("remove_label", [])) == 1
    assert len(applied.get("mark_read", [])) == 1
    assert len(applied.get("accept_invite", [])) == 1
    assert len(applied.get("tentative_invite", [])) == 1


def test_resume_dispatches_by_state_action_never_the_compiled_graph_object():
    """The class of bug the old thread_id-prefix graph selection caused
    (docs/decisions.md): approving one capability's card must never run a
    DIFFERENT capability's apply function. Proven here at the registry/
    graph layer directly — two capabilities, sharing nothing but the same
    compiled graph object, each only ever see their own effect fire."""
    label_calls = []
    mark_read_calls = []
    registry = build_capability_registry(
        label_apply_fn=lambda state: label_calls.append(state) or "labeled",
        mark_read_apply_fn=lambda state: mark_read_calls.append(state) or "read",
    )
    graph = build_draft_approve_graph(
        client=FakeClient(), store=FakeStore(), checkpointer=InMemorySaver(),
        registry=registry,
    )

    for action, ref in (("label", "t1"), ("mark_read", "t2")):
        cfg = {"configurable": {"thread_id": f"same-namespace:{ref}"}}
        graph.invoke(
            {
                "user_id": "u1", "domain": "mail", "action": action,
                "incoming_ref": ref, "incoming_summary": "x",
                "label_name": "Finance", "audit_events": [],
            },
            cfg,
        )
        graph.invoke(Command(resume={"decision": "approved"}), cfg)

    assert len(label_calls) == 1 and label_calls[0]["incoming_ref"] == "t1"
    assert len(mark_read_calls) == 1 and mark_read_calls[0]["incoming_ref"] == "t2"


# ---------------------------------------------------------------------------
# A new capability added ENTIRELY within a test — one descriptor plus a
# fake connector method, no production file touched outside the registry.
# ---------------------------------------------------------------------------


def test_new_capability_added_in_test_suite_touches_no_production_file():
    """Registers a brand-new, test-only capability (not in
    orchestrator.capabilities.build_capability_registry, not a real
    Action the production Settings/connector know about) directly against
    a fake connector method, and proves it participates in the SAME
    generic graph/gate machinery as every production capability — the
    proof that a new capability is one descriptor plus tests, nothing
    else."""

    class _FakeSnoozeConnector:
        def __init__(self):
            self.snoozed: list[str] = []

        def supports_snooze(self):
            return True

        def snooze_thread(self, thread_id):
            self.snoozed.append(thread_id)

    connector = _FakeSnoozeConnector()

    def snooze_apply(state):
        if state.get("action") != Action.SUMMARIZE.value:
            return None
        connector.snooze_thread(state["incoming_ref"])
        return state["incoming_ref"]

    def snooze_propose(client, incoming_summary, memories, domain):
        return incoming_summary

    snooze_capability = Capability(
        action=Action.SUMMARIZE,  # reusing an existing enum value is fine —
        # the point under test is the registry/graph plumbing, not adding a
        # new Action (that's autonomy.py's concern, exercised separately).
        domain=Domain.MAIL,
        risk_tier=RiskTier.R1,
        propose=snooze_propose,
        apply=snooze_apply,
        connector_probe="supports_snooze",
        enabled_flag=None,
        irreversible=True,
    )
    registry = CapabilityRegistry((snooze_capability,))

    matrix = default_matrix().grant(Action.SUMMARIZE, Domain.MAIL, Rung.PROPOSE)
    assert capability_gates_pass(
        snooze_capability, connector=connector, matrix=matrix,
    )

    graph = build_draft_approve_graph(
        client=FakeClient(), store=FakeStore(), checkpointer=InMemorySaver(),
        registry=registry,
    )
    cfg = {"configurable": {"thread_id": "snooze:t1"}}
    graph.invoke(
        {
            "user_id": "u1", "domain": "mail", "action": "summarize",
            "incoming_ref": "t1", "incoming_summary": "snooze this",
            "audit_events": [],
        },
        cfg,
    )
    graph.invoke(Command(resume={"decision": "approved"}), cfg)

    assert connector.snoozed == ["t1"]


# ---------------------------------------------------------------------------
# capability_gates_pass: the three independent gates for mark_read and
# RSVP_ACCEPT (build prompt 30 acceptance) — each refuses independently.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action_name", ["mark_read", "rsvp_accept"])
def test_new_capability_refuses_when_flag_off(action_name):
    registry = build_capability_registry()
    capability = registry.get(action_name)
    assert capability is not None
    assert capability_gates_pass(
        capability, connector=_FullMatrixConnector(), enabled=False,
        matrix=_granted_matrix(),
    ) is False


@pytest.mark.parametrize("action_name", ["mark_read", "rsvp_accept"])
def test_new_capability_refuses_when_connector_lacks_support(action_name):
    registry = build_capability_registry()
    capability = registry.get(action_name)

    class _NoSupportConnector:
        def supports_labeling(self):
            return False

        def supports_calendar_writes(self):
            return False

    assert capability_gates_pass(
        capability, connector=_NoSupportConnector(), enabled=True,
        matrix=_granted_matrix(),
    ) is False


@pytest.mark.parametrize("action_name", ["mark_read", "rsvp_accept"])
def test_new_capability_refuses_when_rung_below_propose(action_name):
    registry = build_capability_registry()
    capability = registry.get(action_name)
    assert capability_gates_pass(
        capability, connector=_FullMatrixConnector(), enabled=True,
        matrix=default_matrix(),  # no grant at all -> READ_ONLY < PROPOSE
    ) is False


@pytest.mark.parametrize("action_name", ["mark_read", "rsvp_accept"])
def test_new_capability_passes_when_all_three_gates_hold(action_name):
    registry = build_capability_registry()
    capability = registry.get(action_name)
    assert capability_gates_pass(
        capability, connector=_FullMatrixConnector(), enabled=True,
        matrix=_granted_matrix(),
    ) is True
