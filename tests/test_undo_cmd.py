"""Tests for ``attune undo`` (cli/undo_cmd.py, build prompt 31, task 2)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from attune.cli.undo_cmd import run_undo
from attune.orchestrator.capabilities import CapabilityRegistry, build_capability_registry
from attune.orchestrator.autonomy import Action

# run_undo (a thin CLI wrapper) has no injectable clock, so decided_at must
# be "just now" relative to the real wall clock for these tests to fall
# inside undo_effect's real UNDO_WINDOW regardless of when they run.
T0 = datetime.now(timezone.utc)


class _FakeLedger:
    def __init__(self, row):
        self._row = row
        self.undone_calls: list[tuple] = []

    def get(self, proposal_id):
        return self._row if proposal_id == self._row.proposal_id else None

    def mark_undone(self, proposal_id, *, at=None):
        self.undone_calls.append((proposal_id, at))
        self._row.undone = True


class _FakeGraph:
    def __init__(self, state):
        self._state = state

    def get_state(self, config):
        return SimpleNamespace(values=self._state)


class _FakeAuditLog:
    def __init__(self):
        self.events = []

    def record(self, **kw):
        self.events.append(kw)


class _FakeApp:
    def __init__(self, *, graph, registry, ledger, audit_log):
        self.graph = graph
        self.registry = registry
        self.ledger = ledger
        self.audit_log = audit_log
        self.settings = SimpleNamespace(user_id="me@example.com")


class _FakeRuntime:
    def __init__(self, app):
        self.app = app


def _row(**overrides):
    defaults = dict(
        proposal_id="archive:t1", action="label", domain="mail",
        decision="approved", applied_ok=True, decided_at=T0, undone=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_run_undo_success_prints_confirmation():
    compensated = []
    label_capability = build_capability_registry().get(Action.LABEL)
    import dataclasses
    label_capability = dataclasses.replace(
        label_capability,
        compensate=lambda state: compensated.append(state["applied_ref"]),
        irreversible=False,
    )
    registry = CapabilityRegistry((label_capability,))
    ledger = _FakeLedger(_row())
    app = _FakeApp(
        graph=_FakeGraph({"applied_ref": "t1"}), registry=registry,
        ledger=ledger, audit_log=_FakeAuditLog(),
    )
    out_lines: list[str] = []

    code = run_undo(
        "archive:t1", runtime_factory=lambda: _FakeRuntime(app),
        out=out_lines.append, actor="cli",
    )

    assert code == 0
    assert compensated == ["t1"]
    assert "Undone" in out_lines[0]
    assert ledger.undone_calls


def test_run_undo_refuses_irreversible_action():
    registry = build_capability_registry()  # SEND_REPLY: irreversible=True
    ledger = _FakeLedger(_row(action="send_reply", proposal_id="gmail:t1:100"))
    app = _FakeApp(
        graph=_FakeGraph({}), registry=registry, ledger=ledger,
        audit_log=_FakeAuditLog(),
    )
    out_lines: list[str] = []

    code = run_undo(
        "gmail:t1:100", runtime_factory=lambda: _FakeRuntime(app),
        out=out_lines.append,
    )

    assert code == 2
    assert "Cannot undo" in out_lines[0]
    assert "irreversible" in out_lines[0]


def test_run_undo_unknown_effect_id_refuses():
    registry = build_capability_registry()
    ledger = _FakeLedger(_row())
    app = _FakeApp(
        graph=_FakeGraph({}), registry=registry, ledger=ledger,
        audit_log=_FakeAuditLog(),
    )
    out_lines: list[str] = []

    code = run_undo(
        "never-registered", runtime_factory=lambda: _FakeRuntime(app),
        out=out_lines.append,
    )

    assert code == 2
    assert "no decision recorded" in out_lines[0]
