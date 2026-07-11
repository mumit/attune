"""Tests for audit/log.py — JsonlAuditLog record/query, no live services.

All I/O is a tmp_path JSONL file; no mocking needed since the module has no
external dependencies.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from aidedecamp.audit.log import AuditEntry, JsonlAuditLog


# ---------------------------------------------------------------------------
# record() + basic persistence
# ---------------------------------------------------------------------------


def test_record_creates_file_and_parent_dirs(tmp_path):
    path = tmp_path / "nested" / "audit.log.jsonl"
    log = JsonlAuditLog(str(path))
    log.record(
        thread_id="t1", workflow="draft_approve",
        events=[{"event": "retrieved", "ts": "2026-07-10T00:00:00+00:00", "count": 3}],
    )
    assert path.exists()


def test_record_writes_one_line_per_event(tmp_path):
    path = tmp_path / "audit.log.jsonl"
    log = JsonlAuditLog(str(path))
    log.record(
        thread_id="t1", workflow="draft_approve",
        events=[
            {"event": "retrieved", "ts": "2026-07-10T00:00:00+00:00"},
            {"event": "drafted", "ts": "2026-07-10T00:00:01+00:00"},
        ],
    )
    lines = path.read_text().strip().split("\n")
    assert len(lines) == 2


def test_record_stamps_thread_id_workflow_domain_user(tmp_path):
    path = tmp_path / "audit.log.jsonl"
    log = JsonlAuditLog(str(path))
    log.record(
        thread_id="gmail:t1:100", workflow="draft_approve",
        events=[{"event": "drafted", "ts": "2026-07-10T00:00:00+00:00", "chars": 42}],
        domain="mail", user_id="me@example.com",
    )
    line = json.loads(path.read_text().strip())
    assert line["thread_id"] == "gmail:t1:100"
    assert line["workflow"] == "draft_approve"
    assert line["domain"] == "mail"
    assert line["user_id"] == "me@example.com"
    assert line["event"] == "drafted"
    assert line["chars"] == 42


def test_record_appends_across_calls(tmp_path):
    path = tmp_path / "audit.log.jsonl"
    log = JsonlAuditLog(str(path))
    log.record(thread_id="t1", workflow="w", events=[{"event": "a", "ts": "2026-07-10T00:00:00+00:00"}])
    log.record(thread_id="t2", workflow="w", events=[{"event": "b", "ts": "2026-07-10T00:00:01+00:00"}])
    lines = path.read_text().strip().split("\n")
    assert len(lines) == 2


def test_record_defaults_ts_when_missing(tmp_path):
    path = tmp_path / "audit.log.jsonl"
    log = JsonlAuditLog(str(path))
    log.record(thread_id="t1", workflow="w", events=[{"event": "no_ts"}])
    line = json.loads(path.read_text().strip())
    assert line["ts"]  # non-empty, filled in


# ---------------------------------------------------------------------------
# query()
# ---------------------------------------------------------------------------


def _seed(log: JsonlAuditLog):
    log.record(
        thread_id="gmail:t1:100", workflow="draft_approve",
        events=[{"event": "retrieved", "ts": "2026-07-10T00:00:00+00:00"}],
        domain="mail", user_id="alice",
    )
    log.record(
        thread_id="gmail:t2:100", workflow="draft_approve",
        events=[{"event": "drafted", "ts": "2026-07-10T01:00:00+00:00"}],
        domain="mail", user_id="bob",
    )
    log.record(
        thread_id="chat:s1", workflow="converse",
        events=[{"event": "answered", "ts": "2026-07-10T02:00:00+00:00"}],
        domain="chat", user_id="alice",
    )


def test_query_returns_all_when_no_filters(tmp_path):
    log = JsonlAuditLog(str(tmp_path / "a.jsonl"))
    _seed(log)
    assert len(log.query()) == 3


def test_query_returns_empty_list_when_file_missing(tmp_path):
    log = JsonlAuditLog(str(tmp_path / "missing.jsonl"))
    assert log.query() == []


def test_query_filters_by_thread_id(tmp_path):
    log = JsonlAuditLog(str(tmp_path / "a.jsonl"))
    _seed(log)
    results = log.query(thread_id="gmail:t1:100")
    assert len(results) == 1
    assert results[0].event == "retrieved"


def test_query_filters_by_domain(tmp_path):
    log = JsonlAuditLog(str(tmp_path / "a.jsonl"))
    _seed(log)
    results = log.query(domain="mail")
    assert len(results) == 2
    assert all(r.domain == "mail" for r in results)


def test_query_filters_by_user_id(tmp_path):
    log = JsonlAuditLog(str(tmp_path / "a.jsonl"))
    _seed(log)
    results = log.query(user_id="alice")
    assert len(results) == 2
    assert all(r.user_id == "alice" for r in results)


def test_query_filters_by_since(tmp_path):
    log = JsonlAuditLog(str(tmp_path / "a.jsonl"))
    _seed(log)
    since = datetime(2026, 7, 10, 0, 30, tzinfo=timezone.utc)
    results = log.query(since=since)
    assert len(results) == 2
    assert all(r.event != "retrieved" for r in results)


def test_query_respects_limit_keeping_most_recent(tmp_path):
    log = JsonlAuditLog(str(tmp_path / "a.jsonl"))
    _seed(log)
    results = log.query(limit=1)
    assert len(results) == 1
    assert results[0].event == "answered"


def test_query_combines_filters(tmp_path):
    log = JsonlAuditLog(str(tmp_path / "a.jsonl"))
    _seed(log)
    results = log.query(domain="mail", user_id="bob")
    assert len(results) == 1
    assert results[0].thread_id == "gmail:t2:100"


# ---------------------------------------------------------------------------
# AuditEntry round-trip
# ---------------------------------------------------------------------------


def test_entry_to_json_from_json_roundtrip():
    entry = AuditEntry(
        thread_id="t1", workflow="w", event="e", ts="2026-07-10T00:00:00+00:00",
        domain="mail", user_id="u1", fields={"extra": "value"},
    )
    restored = AuditEntry.from_json(entry.to_json())
    assert restored == entry


def test_entry_fields_extracted_from_unknown_keys():
    raw = {
        "thread_id": "t1", "workflow": "w", "event": "e", "ts": "2026-07-10T00:00:00+00:00",
        "domain": None, "user_id": None, "custom_field": 123,
    }
    entry = AuditEntry.from_json(raw)
    assert entry.fields == {"custom_field": 123}
