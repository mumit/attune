"""Tests for the bounded hosted protocol-retention entry point."""

from __future__ import annotations

import pytest

from attune.hosted import protocol_retention


class _Cursor:
    def __init__(self, row=(1, 2, 3, 4)):
        self.row = row
        self.executed = None

    def execute(self, sql, parameters):
        self.executed = (sql, parameters)

    def fetchone(self):
        return self.row

    def close(self):
        return None


class _Connection:
    def __init__(self, row=(1, 2, 3, 4)):
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


def test_protocol_retention_is_bounded_and_returns_content_free_counts(monkeypatch):
    connection = _Connection()
    monkeypatch.setattr(protocol_retention, "iam_connection", lambda: connection)

    assert protocol_retention.run_protocol_retention(batch_size=25) == {
        "oauth_transactions": 1,
        "channel_setup_transactions": 2,
        "identity_sessions": 3,
        "provider_events": 4,
    }
    sql, parameters = connection.cursor_instance.executed
    assert sql == "SELECT * FROM attune.prune_expired_protocol_records(%s, %s)"
    assert parameters[1] == 25
    assert connection.commits == 1
    assert connection.rollbacks == 0


@pytest.mark.parametrize("batch_size", [0, 1001, True, 1.5])
def test_protocol_retention_rejects_invalid_batch_size(batch_size):
    with pytest.raises((TypeError, ValueError)):
        protocol_retention.run_protocol_retention(batch_size=batch_size)


def test_protocol_retention_rolls_back_invalid_database_result(monkeypatch):
    connection = _Connection(row=None)
    monkeypatch.setattr(protocol_retention, "iam_connection", lambda: connection)

    with pytest.raises(RuntimeError, match="invalid result"):
        protocol_retention.run_protocol_retention()
    assert connection.commits == 0
    assert connection.rollbacks == 1
