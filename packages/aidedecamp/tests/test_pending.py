"""Tests for orchestrator/pending.py — the pending-approvals registry and the
IGNORED-signal sweep (design 2.2, roadmap prompt 03). All offline: file-backed
registry in tmp_path, fake MemoryStore/audit log, injected clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aidedecamp.memory.base import MemoryStore
from aidedecamp.orchestrator import JsonPendingApprovals, sweep_ignored


class FakeStore(MemoryStore):
    def __init__(self):
        self.added: list[dict] = []

    def add(self, messages, *, user_id, metadata=None, infer=True):
        self.added.append(
            {"messages": messages, "metadata": metadata, "infer": infer}
        )
        return []

    def search(self, query, *, user_id, limit=8, min_score=None):
        return []

    def get_all(self, *, user_id, limit=100):
        return []

    def delete(self, memory_id):
        pass


class FakeAuditLog:
    def __init__(self):
        self.recorded: list[dict] = []

    def record(self, **kwargs):
        self.recorded.append(kwargs)


T0 = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)


def _registry(tmp_path):
    return JsonPendingApprovals(str(tmp_path / "pending.json"))


# ---------------------------------------------------------------------------
# Registry mechanics
# ---------------------------------------------------------------------------


def test_register_and_lookup_by_source(tmp_path):
    reg = _registry(tmp_path)
    reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)

    entry = reg.get_pending_for_source("t1")
    assert entry is not None
    assert entry.lg_tid == "gmail:t1:100"
    assert entry.posted_at == T0
    assert reg.get_pending_for_source("t2") is None


def test_resolve_removes_from_pending(tmp_path):
    reg = _registry(tmp_path)
    reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)
    reg.resolve("gmail:t1:100")

    assert reg.get_pending_for_source("t1") is None
    assert reg.pending() == []


def test_resolve_unknown_id_is_noop(tmp_path):
    # Resume paths call resolve unconditionally, including for workflows
    # (e.g. chat-domain ones) that were never registered.
    _registry(tmp_path).resolve("never-registered")


def test_claim_is_single_use_and_records_actor(tmp_path):
    import json

    path = tmp_path / "pending.json"
    reg = JsonPendingApprovals(str(path))
    reg.register(
        lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0
    )

    assert reg.claim("gmail:t1:100", actor="U-OWNER") is True
    assert reg.claim("gmail:t1:100", actor="U-OWNER") is False
    raw = json.loads(path.read_text())["gmail:t1:100"]
    assert raw["resolved_by"] == "U-OWNER"


def test_claim_unknown_workflow_is_unmanaged(tmp_path):
    assert _registry(tmp_path).claim("not-registered", actor="U1") is None


def test_round_trips_through_file(tmp_path):
    path = str(tmp_path / "pending.json")
    JsonPendingApprovals(path).register(
        lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0
    )
    # A fresh instance (fresh process, in production) reads the same state —
    # and the sweep's age math consumes the parsed posted_at correctly.
    reloaded = JsonPendingApprovals(path)
    entry = reloaded.get_pending_for_source("t1")
    assert entry.posted_at == T0
    swept = sweep_ignored(
        reloaded, FakeStore(), user_id="u1", now=T0 + timedelta(hours=49)
    )
    assert swept == 1


# ---------------------------------------------------------------------------
# sweep_ignored
# ---------------------------------------------------------------------------


def test_sweep_captures_ignored_after_max_age(tmp_path):
    reg = _registry(tmp_path)
    store = FakeStore()
    audit = FakeAuditLog()
    reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)

    swept = sweep_ignored(
        reg, store, user_id="u1", now=T0 + timedelta(hours=49), audit_log=audit
    )

    assert swept == 1
    assert len(store.added) == 1
    meta = store.added[0]["metadata"]
    assert meta["action"] == "ignored"
    assert meta["source_ref"] == "t1"
    assert store.added[0]["infer"] is False  # raw signal, verbatim
    assert audit.recorded[0]["events"][0]["event"] == "approval_ignored"
    assert audit.recorded[0]["thread_id"] == "gmail:t1:100"


def test_sweep_leaves_fresh_entries_alone(tmp_path):
    reg = _registry(tmp_path)
    store = FakeStore()
    reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)

    swept = sweep_ignored(reg, store, user_id="u1", now=T0 + timedelta(hours=47))

    assert swept == 0
    assert store.added == []
    assert reg.get_pending_for_source("t1") is not None


def test_sweep_captures_each_entry_exactly_once(tmp_path):
    reg = _registry(tmp_path)
    store = FakeStore()
    reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)

    late = T0 + timedelta(hours=72)
    assert sweep_ignored(reg, store, user_id="u1", now=late) == 1
    assert sweep_ignored(reg, store, user_id="u1", now=late) == 0
    assert len(store.added) == 1


def test_sweep_respects_custom_max_age(tmp_path):
    reg = _registry(tmp_path)
    store = FakeStore()
    reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)

    swept = sweep_ignored(
        reg, store, user_id="u1", max_age=timedelta(hours=2),
        now=T0 + timedelta(hours=3),
    )
    assert swept == 1


def test_sweep_marks_ignored_not_resolved(tmp_path):
    """Prompt 21: the registry's status is honest — expired-unanswered is
    'ignored', distinct from a human's 'resolved'."""
    import json as _json

    path = tmp_path / "pending.json"
    reg = JsonPendingApprovals(str(path))
    reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)

    sweep_ignored(reg, FakeStore(), user_id="u1", now=T0 + timedelta(hours=49))

    raw = _json.loads(path.read_text())
    assert raw["gmail:t1:100"]["status"] == "ignored"
    # a late human click still flips it to resolved
    reg.resolve("gmail:t1:100")
    raw = _json.loads(path.read_text())
    assert raw["gmail:t1:100"]["status"] == "resolved"


def test_resolved_entry_never_swept(tmp_path):
    reg = _registry(tmp_path)
    store = FakeStore()
    reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)
    reg.resolve("gmail:t1:100")  # user answered the card

    swept = sweep_ignored(reg, store, user_id="u1", now=T0 + timedelta(days=30))
    assert swept == 0
    assert store.added == []
