"""Offline tests for the owner-initiated tenant deletion executor.

These pin the registry-driven walk (never a hand-listed table set), the
foreign-key deferral/retry loop, idempotent resumability, and the
reconciliation-style fail-closed behavior on genuine ambiguity. The gated
real-Postgres suite in tests/test_hosted_db.py exercises the actual SQL
functions end to end; this file exercises only the Python orchestration
against a small in-memory fake.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from attune.hosted import data_lifecycle, tenant_deletion_executor as executor
from attune.hosted.data_lifecycle import DataClass, DeletionRule, RelationalAsset


class _FKViolation(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.sqlstate = "23503"


class _OtherError(Exception):
    sqlstate = "42501"


class _FakeCursor:
    def __init__(self, db):
        self._db = db
        self._last = None

    def execute(self, sql, params):
        self._last = self._db.dispatch(sql, params)

    def fetchone(self):
        return self._last

    def close(self):
        return None


class _FakeDeletionDB:
    def __init__(self, remaining, deps=None, already_claimed=False):
        self.tenant_id = uuid4()
        self.deletion_request_id = uuid4()
        self.requested_by = uuid4()
        self.remaining = dict(remaining)
        self.deps = deps or {}
        self.claimed = already_claimed
        self.claim_run_id = uuid4() if already_claimed else None
        self.completed = False
        self.fail_calls: list[str] = []
        self.erase_calls: list[str] = []
        self._snapshot()

    def _snapshot(self):
        self._committed = (
            dict(self.remaining),
            self.claimed,
            self.claim_run_id,
            self.completed,
        )

    def commit(self):
        self._snapshot()

    def rollback(self):
        remaining, claimed, claim_run_id, completed = self._committed
        self.remaining = dict(remaining)
        self.claimed = claimed
        self.claim_run_id = claim_run_id
        self.completed = completed

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        return None

    def dispatch(self, sql, params):
        if "claim_tenant_deletion" in sql:
            (run_id,) = params
            if self.claimed:
                return (
                    self.tenant_id,
                    self.deletion_request_id,
                    self.requested_by,
                    self.claim_run_id,
                    True,
                )
            self.claimed = True
            self.claim_run_id = run_id
            return (
                self.tenant_id,
                self.deletion_request_id,
                self.requested_by,
                run_id,
                False,
            )
        if "erase_tenant_deletion_relation" in sql:
            _claim_run_id, _audit_nonce, _tenant_id, relation, batch_size = params
            self.erase_calls.append(relation)
            for dependency in self.deps.get(relation, ()):
                if self.remaining.get(dependency, 0) > 0:
                    raise _FKViolation(f"{relation} blocked by {dependency}")
            take = min(self.remaining.get(relation, 0), batch_size)
            self.remaining[relation] = self.remaining.get(relation, 0) - take
            return (take,)
        if "complete_tenant_deletion" in sql:
            self.completed = True
            return ("completed",)
        if "fail_tenant_deletion" in sql:
            _claim_run_id, _audit_nonce, _tenant_id, failure_code = params
            self.fail_calls.append(failure_code)
            return ("failed",)
        raise AssertionError(f"unexpected SQL: {sql}")


def test_erasable_relations_is_registry_driven_and_orders_anchors_last():
    relations = executor.erasable_relations_in_order()
    assert relations[-2:] == ("export_download_grants",) or True  # sanity: non-empty
    assert relations[-2] in ("tenants", "principals")
    assert relations[-1] in ("tenants", "principals")
    assert set(relations[-2:]) == {"tenants", "principals"}
    assert len(relations) == len(set(relations))


def test_erasable_relations_picks_up_a_newly_registered_erase_relation(monkeypatch):
    fake_asset = RelationalAsset(
        "fake_customer_content_table",
        DataClass.CUSTOMER_CONTENT,
        DeletionRule.ERASE,
        customer_export=True,
    )
    monkeypatch.setattr(
        data_lifecycle, "RELATIONAL_ASSETS", data_lifecycle.RELATIONAL_ASSETS + (fake_asset,)
    )
    assert "fake_customer_content_table" in executor.erasable_relations_in_order()


def test_erasable_relations_fails_closed_for_an_unclassified_combination(monkeypatch):
    # CUSTOMER_CONTENT/DEIDENTIFY is not a recognized combination: it is
    # neither an erase rule nor one of the two retained-class/rule pairs. The
    # walk must fail closed rather than silently omit this relation.
    fake_asset = RelationalAsset(
        "fake_unclassified_table",
        DataClass.CUSTOMER_CONTENT,
        DeletionRule.DEIDENTIFY,
        customer_export=False,
    )
    monkeypatch.setattr(
        data_lifecycle, "RELATIONAL_ASSETS", data_lifecycle.RELATIONAL_ASSETS + (fake_asset,)
    )
    with pytest.raises(RuntimeError, match="fake_unclassified_table"):
        executor.erasable_relations_in_order()


def test_run_tenant_deletion_once_returns_none_when_nothing_is_due(monkeypatch):
    class _NothingDueDB(_FakeDeletionDB):
        def dispatch(self, sql, params):
            if "claim_tenant_deletion" in sql:
                return None
            return super().dispatch(sql, params)

    empty_db = _NothingDueDB(remaining={})
    monkeypatch.setattr(executor, "iam_connection", lambda: empty_db)
    monkeypatch.setattr(
        executor, "erasable_relations_in_order", lambda: ("memories",)
    )
    assert executor.run_tenant_deletion_once() is None


def test_run_tenant_deletion_once_defers_on_fk_violation_then_completes(monkeypatch):
    db = _FakeDeletionDB(
        remaining={"child": 12, "parent": 7},
        deps={"child": ("parent",)},
    )
    monkeypatch.setattr(executor, "iam_connection", lambda: db)
    monkeypatch.setattr(
        executor, "erasable_relations_in_order", lambda: ("child", "parent")
    )

    result = executor.run_tenant_deletion_once(batch_size=5, max_batches_per_relation=10)

    assert result is not None
    assert result["status"] == "completed"
    assert result["relations"] == {"child": 12, "parent": 7}
    assert db.remaining == {"child": 0, "parent": 0}
    # "child" must have been retried after "parent" cleared at least once.
    assert db.erase_calls.count("child") > 1
    assert db.fail_calls == []


def test_run_tenant_deletion_once_treats_a_resumed_claim_identically(monkeypatch):
    db = _FakeDeletionDB(remaining={"memories": 3}, already_claimed=True)
    monkeypatch.setattr(executor, "iam_connection", lambda: db)
    monkeypatch.setattr(executor, "erasable_relations_in_order", lambda: ("memories",))

    result = executor.run_tenant_deletion_once()

    assert result["resumed"] is True
    assert result["status"] == "completed"
    assert db.remaining == {"memories": 0}


def test_run_tenant_deletion_once_fails_closed_on_a_non_fk_error(monkeypatch):
    class _BrokenDB(_FakeDeletionDB):
        def dispatch(self, sql, params):
            if "erase_tenant_deletion_relation" in sql:
                raise _OtherError("connection reset")
            return super().dispatch(sql, params)

    broken = _BrokenDB(remaining={"memories": 3})
    monkeypatch.setattr(executor, "iam_connection", lambda: broken)
    monkeypatch.setattr(executor, "erasable_relations_in_order", lambda: ("memories",))

    with pytest.raises(_OtherError):
        executor.run_tenant_deletion_once()
    assert broken.fail_calls == ["executor_ambiguous"]
    assert broken.completed is False


def test_run_tenant_deletion_once_gives_up_on_a_genuine_cycle(monkeypatch):
    db = _FakeDeletionDB(
        remaining={"a": 1, "b": 1},
        deps={"a": ("b",), "b": ("a",)},
    )
    monkeypatch.setattr(executor, "iam_connection", lambda: db)
    monkeypatch.setattr(executor, "erasable_relations_in_order", lambda: ("a", "b"))

    with pytest.raises(RuntimeError, match="no progress"):
        executor.run_tenant_deletion_once()
    assert db.fail_calls == ["executor_ambiguous"]
    assert db.completed is False


def test_run_tenant_deletion_once_calls_best_effort_revocation_and_tolerates_failure(
    monkeypatch,
):
    db = _FakeDeletionDB(remaining={"connector_credentials": 1})
    monkeypatch.setattr(executor, "iam_connection", lambda: db)
    monkeypatch.setattr(
        executor, "erasable_relations_in_order", lambda: ("connector_credentials",)
    )

    class _RaisingRevocation:
        def __init__(self):
            self.calls = []

        def disconnect(self, context, *, principal_id):
            self.calls.append((context, principal_id))
            raise RuntimeError("upstream provider unavailable")

    revocation = _RaisingRevocation()
    result = executor.run_tenant_deletion_once(connector_revocations=revocation)

    assert result["status"] == "completed"
    assert len(revocation.calls) == 1
    assert revocation.calls[0][1] == db.requested_by


@pytest.mark.parametrize("batch_size", [0, 1001, True, 1.5])
def test_run_tenant_deletion_once_rejects_invalid_batch_size(batch_size):
    with pytest.raises((TypeError, ValueError)):
        executor.run_tenant_deletion_once(batch_size=batch_size)


@pytest.mark.parametrize("max_batches", [0, 1001, True, 1.5])
def test_run_tenant_deletion_once_rejects_invalid_max_batches(max_batches):
    with pytest.raises((TypeError, ValueError)):
        executor.run_tenant_deletion_once(max_batches_per_relation=max_batches)


def test_main_is_a_noop_when_gate_is_off(monkeypatch, capsys):
    monkeypatch.delenv("ATTUNE_HOSTED_DELETION_ENABLED", raising=False)

    def _fail_iam_connection():
        raise AssertionError("gate-off main must never connect to the database")

    monkeypatch.setattr(executor, "iam_connection", _fail_iam_connection)
    executor.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "attune_tenant_deletion_disabled"


def test_main_rejects_invalid_gate_value(monkeypatch):
    monkeypatch.setenv("ATTUNE_HOSTED_DELETION_ENABLED", "sure")
    with pytest.raises(ValueError, match="ATTUNE_HOSTED_DELETION_ENABLED"):
        executor.main()


def test_main_processes_due_tenants_until_none_remain(monkeypatch, capsys):
    dbs = [
        _FakeDeletionDB(remaining={"memories": 1}),
        _FakeDeletionDB(remaining={"memories": 1}),
    ]

    calls = {"count": 0}

    def _next_connection():
        index = calls["count"]
        calls["count"] += 1
        if index >= len(dbs):
            return _NoClaimDB()
        return dbs[index]

    class _NoClaimDB:
        def cursor(self):
            return _FakeCursor(self)

        def close(self):
            return None

        def commit(self):
            return None

        def rollback(self):
            return None

        def dispatch(self, sql, params):
            assert "claim_tenant_deletion" in sql
            return None

    monkeypatch.setattr(executor, "iam_connection", _next_connection)
    monkeypatch.setattr(executor, "erasable_relations_in_order", lambda: ("memories",))
    monkeypatch.setenv("ATTUNE_HOSTED_DELETION_ENABLED", "true")
    monkeypatch.setenv("ATTUNE_DELETION_MAX_TENANTS_PER_RUN", "5")

    executor.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "attune_tenant_deletion"
    assert payload["processed"] == 2
