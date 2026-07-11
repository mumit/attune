"""Tests for the runtime assembly (app.py).

All tests run offline: SqliteSaver is replaced by InMemorySaver, Mem0Store by
a FakeStore, and the Fuel iX client by a FakeClient. The real wiring logic —
graph compilation, settings pass-through, context-manager lifecycle — is fully
exercised without any live service.
"""

from __future__ import annotations

import pytest

from aidedecamp.app import AppContext, build_app
from aidedecamp.config import Deployment, Settings
from aidedecamp.memory.base import MemoryRecord, MemoryStore

langgraph = pytest.importorskip("langgraph")
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fakes (same shape as test_orchestrator.py)
# ---------------------------------------------------------------------------


class FakeStore(MemoryStore):
    def add(self, messages, *, user_id, metadata=None, infer=True):
        return []

    def search(self, query, *, user_id, limit=8, min_score=None):
        return [MemoryRecord(id="m1", text="prefers brief replies", score=0.9)]

    def get_all(self, *, user_id, limit=100):
        return []

    def delete(self, memory_id):
        pass


class _FakeMsg:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})


class _FakeResp:
    def __init__(self, content):
        self.choices = [_FakeMsg(content)]


class FakeClient:
    def chat_completions_create(self, **kwargs):
        return _FakeResp("Short reply.")


def _fake_app(**kwargs):
    return build_app(
        client=FakeClient(),
        store=FakeStore(),
        checkpointer=InMemorySaver(),
        **kwargs,
    )


def _state(**over):
    return {
        "user_id": "u1",
        "domain": "mail",
        "action": "draft_reply",
        "incoming_ref": "ref-1",
        "incoming_summary": "Quick question about the meeting.",
        "audit_events": [],
        "iteration_count": 0,
        **over,
    }


# ---------------------------------------------------------------------------
# Assembly tests
# ---------------------------------------------------------------------------


def test_build_app_returns_app_context():
    app = _fake_app()
    assert isinstance(app, AppContext)


def test_build_app_wires_all_collaborators():
    app = _fake_app()
    assert app.graph is not None
    assert app.client is not None
    assert app.store is not None
    assert app.settings is not None


def test_build_app_uses_provided_settings():
    settings = Settings.from_env({"ADC_DEPLOYMENT": "telus"})
    app = _fake_app(settings=settings)
    assert app.settings.deployment == Deployment.TELUS


def test_build_app_no_db_conn_when_checkpointer_injected():
    app = _fake_app()
    assert app._db_conn is None


# ---------------------------------------------------------------------------
# Graph behaviour (wiring is correct when the graph runs end-to-end)
# ---------------------------------------------------------------------------


def test_graph_pauses_for_human_approval():
    app = _fake_app()
    result = app.graph.invoke(_state(), {"configurable": {"thread_id": "t1"}})
    assert "__interrupt__" in result
    assert result["__interrupt__"][0].value["question"] == "Approve this draft?"


def test_graph_completes_after_approve():
    app = _fake_app()
    cfg = {"configurable": {"thread_id": "t-approve"}}
    app.graph.invoke(_state(), cfg)
    out = app.graph.invoke(Command(resume={"decision": "approved"}), cfg)
    assert out["decision"] == "approved"
    assert out["final_text"]


def test_graph_completes_after_reject():
    app = _fake_app()
    cfg = {"configurable": {"thread_id": "t-reject"}}
    app.graph.invoke(_state(), cfg)
    out = app.graph.invoke(Command(resume={"decision": "rejected"}), cfg)
    assert out["decision"] == "rejected"
    assert out.get("final_text") is None


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


def test_close_is_noop_without_db_conn():
    app = _fake_app()
    app.close()  # must not raise
    app.close()  # idempotent


def test_close_calls_conn_close():
    closed: list[bool] = []

    class _FakeConn:
        def close(self):
            closed.append(True)

    app = AppContext(
        graph=None,
        client=None,
        store=FakeStore(),
        settings=Settings.from_env(),
        _db_conn=_FakeConn(),
    )
    app.close()
    assert closed == [True]
    assert app._db_conn is None


def test_close_is_idempotent_on_real_conn():
    closed: list[bool] = []

    class _FakeConn:
        def close(self):
            closed.append(True)

    app = AppContext(
        graph=None,
        client=None,
        store=FakeStore(),
        settings=Settings.from_env(),
        _db_conn=_FakeConn(),
    )
    app.close()
    app.close()
    assert len(closed) == 1


def test_context_manager_closes_conn():
    closed: list[bool] = []

    class _FakeConn:
        def close(self):
            closed.append(True)

    app = AppContext(
        graph=None,
        client=None,
        store=FakeStore(),
        settings=Settings.from_env(),
        _db_conn=_FakeConn(),
    )
    with app:
        pass
    assert closed == [True]


def test_context_manager_graph_runs():
    with _fake_app() as app:
        result = app.graph.invoke(_state(), {"configurable": {"thread_id": "t-ctx"}})
    assert "__interrupt__" in result
