"""Tests for the bounded hosted content-retention entry point."""

from __future__ import annotations

import json

import pytest

from attune.hosted import content_retention


class _Cursor:
    def __init__(self, row=(1, 2, 3)):
        self.row = row
        self.executed = None

    def execute(self, sql, parameters):
        self.executed = (sql, parameters)

    def fetchone(self):
        return self.row

    def close(self):
        return None


class _Connection:
    def __init__(self, row=(1, 2, 3)):
        self.cursor_instance = _Cursor(row)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        return None


def test_content_retention_is_bounded_and_returns_content_free_counts(monkeypatch):
    connection = _Connection()
    monkeypatch.setattr(content_retention, "iam_connection", lambda: connection)

    assert content_retention.run_content_retention(batch_size=25) == {
        "conversation_turns": 1,
        "conversations": 2,
        "hosted_brief_deliveries": 3,
        "batches": 1,
        "backlog_possible": False,
    }
    sql, parameters = connection.cursor_instance.executed
    assert sql == "SELECT * FROM attune.prune_expired_customer_content(%s, %s)"
    assert parameters[1] == 25
    assert connection.commits == 1
    assert connection.rollbacks == 0


@pytest.mark.parametrize("batch_size", [0, 1001, True, 1.5])
def test_content_retention_rejects_invalid_batch_size(batch_size):
    with pytest.raises((TypeError, ValueError)):
        content_retention.run_content_retention(batch_size=batch_size)


def test_content_retention_rolls_back_invalid_database_result(monkeypatch):
    connection = _Connection(row=None)
    monkeypatch.setattr(content_retention, "iam_connection", lambda: connection)

    with pytest.raises(RuntimeError, match="invalid result"):
        content_retention.run_content_retention()
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_content_retention_bounds_saturated_batches_and_signals_backlog(monkeypatch):
    connection = _Connection(row=(10, 0, 0))
    monkeypatch.setattr(content_retention, "iam_connection", lambda: connection)

    result = content_retention.run_content_retention(batch_size=10, max_batches=2)
    assert result["conversation_turns"] == 20
    assert result["batches"] == 2
    assert result["backlog_possible"] is True


@pytest.mark.parametrize("max_batches", [0, 11, True, 1.5])
def test_content_retention_rejects_invalid_max_batches(max_batches):
    with pytest.raises((TypeError, ValueError)):
        content_retention.run_content_retention(max_batches=max_batches)


def test_content_retention_main_is_a_content_free_noop_when_gate_is_off(
    monkeypatch, capsys
):
    monkeypatch.delenv("ATTUNE_ENABLE_CONTENT_RETENTION", raising=False)

    def _fail_iam_connection():
        raise AssertionError("gate-off main must never connect to the database")

    monkeypatch.setattr(content_retention, "iam_connection", _fail_iam_connection)
    content_retention.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "attune_content_retention_disabled"


def test_content_retention_main_rejects_invalid_gate_value(monkeypatch):
    monkeypatch.setenv("ATTUNE_ENABLE_CONTENT_RETENTION", "yes")
    with pytest.raises(ValueError, match="ATTUNE_ENABLE_CONTENT_RETENTION"):
        content_retention.main()


def test_content_retention_main_runs_when_gate_is_on(monkeypatch, capsys):
    connection = _Connection()
    monkeypatch.setattr(content_retention, "iam_connection", lambda: connection)
    monkeypatch.setenv("ATTUNE_ENABLE_CONTENT_RETENTION", "true")
    monkeypatch.setenv("ATTUNE_CONTENT_RETENTION_BATCH_SIZE", "50")
    monkeypatch.setenv("ATTUNE_CONTENT_RETENTION_MAX_BATCHES", "1")
    content_retention.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "attune_content_retention"
    assert payload["conversation_turns"] == 1
