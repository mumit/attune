"""Tests for the draft-and-approve orchestrator graph.

A fake Fuel iX client and a fake MemoryStore keep these tests free of any live
model or vector store, while exercising the real graph: retrieval, drafting, the
autonomy gate's routing, the human-approval interrupt, resume, and signal
capture.
"""

from __future__ import annotations

import pytest

from aidedecamp.memory.base import MemoryRecord, MemoryStore
from aidedecamp.orchestrator import (
    Action,
    Domain,
    Rung,
    build_draft_approve_graph,
    default_matrix,
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
