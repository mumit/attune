"""Tests for audit/log.py — JsonlAuditLog record/query, no live services.

All I/O is a tmp_path JSONL file; no mocking needed since the module has no
external dependencies.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from attune.audit.log import AuditEntry, JsonlAuditLog


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


def test_record_chmods_file_owner_only(tmp_path):
    """Security finding F5 (Low): the audit log is append-mode, so the file
    is created under whatever umask the process has; re-assert 0600 after
    every append rather than trust it."""
    import os

    path = tmp_path / "audit.log.jsonl"
    log = JsonlAuditLog(str(path))
    log.record(
        thread_id="t1", workflow="draft_approve",
        events=[{"event": "retrieved", "ts": "2026-07-10T00:00:00+00:00"}],
    )
    assert (os.stat(path).st_mode & 0o777) == 0o600

    # A second append must not regress permissions (e.g. by not touching
    # them at all).
    log.record(
        thread_id="t1", workflow="draft_approve",
        events=[{"event": "drafted", "ts": "2026-07-10T00:00:01+00:00"}],
    )
    assert (os.stat(path).st_mode & 0o777) == 0o600


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


# ---------------------------------------------------------------------------
# hash chain (security finding F1)
# ---------------------------------------------------------------------------


def _lines(path):
    return [json.loads(line) for line in path.read_text().strip().split("\n")]


def test_verify_ok_on_missing_file(tmp_path):
    log = JsonlAuditLog(str(tmp_path / "missing.jsonl"))
    result = log.verify()
    assert result.ok is True
    assert result.checked == 0
    assert result.legacy == 0


def test_record_appends_hash_fields(tmp_path):
    path = tmp_path / "audit.log.jsonl"
    log = JsonlAuditLog(str(path))
    log.record(thread_id="t1", workflow="w", events=[
        {"event": "a", "ts": "2026-07-10T00:00:00+00:00"},
    ])
    line = _lines(path)[0]
    assert line["prev_hash"] == "0" * 64
    assert len(line["entry_hash"]) == 64


def test_append_then_verify_roundtrip_across_multiple_calls_and_events(tmp_path):
    path = tmp_path / "audit.log.jsonl"
    log = JsonlAuditLog(str(path))
    log.record(
        thread_id="t1", workflow="w",
        events=[
            {"event": "a", "ts": "2026-07-10T00:00:00+00:00"},
            {"event": "b", "ts": "2026-07-10T00:00:01+00:00"},
        ],
    )
    log.record(thread_id="t2", workflow="w", events=[
        {"event": "c", "ts": "2026-07-10T00:00:02+00:00"},
    ])

    lines = _lines(path)
    assert len(lines) == 3
    assert lines[0]["prev_hash"] == "0" * 64
    assert lines[1]["prev_hash"] == lines[0]["entry_hash"]
    assert lines[2]["prev_hash"] == lines[1]["entry_hash"]

    result = log.verify()
    assert result.ok is True
    assert result.checked == 3
    assert result.legacy == 0
    assert result.first_bad_line is None


def test_verify_detects_edited_middle_line(tmp_path):
    path = tmp_path / "audit.log.jsonl"
    log = JsonlAuditLog(str(path))
    log.record(thread_id="t1", workflow="w", events=[
        {"event": "a", "ts": "2026-07-10T00:00:00+00:00"},
        {"event": "b", "ts": "2026-07-10T00:00:01+00:00"},
        {"event": "c", "ts": "2026-07-10T00:00:02+00:00"},
    ])
    lines = _lines(path)
    lines[1]["event"] = "tampered"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

    result = log.verify()
    assert result.ok is False
    assert result.first_bad_line == 2
    assert "entry_hash" in result.reason


def test_verify_detects_deleted_middle_line(tmp_path):
    path = tmp_path / "audit.log.jsonl"
    log = JsonlAuditLog(str(path))
    log.record(thread_id="t1", workflow="w", events=[
        {"event": "a", "ts": "2026-07-10T00:00:00+00:00"},
        {"event": "b", "ts": "2026-07-10T00:00:01+00:00"},
        {"event": "c", "ts": "2026-07-10T00:00:02+00:00"},
    ])
    lines = _lines(path)
    del lines[1]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

    result = log.verify()
    assert result.ok is False
    assert result.first_bad_line == 2  # now-second line's prev_hash is stale
    assert "prev_hash" in result.reason


def test_verify_tolerates_legacy_prefix_then_chains_new_records(tmp_path):
    path = tmp_path / "audit.log.jsonl"
    # Pre-existing unhashed lines, written the way the pre-F1 code did.
    legacy = [
        {"thread_id": "t0", "workflow": "w", "event": "old", "ts": "2026-07-01T00:00:00+00:00",
         "domain": None, "user_id": None},
    ]
    path.write_text("\n".join(json.dumps(line) for line in legacy) + "\n")

    log = JsonlAuditLog(str(path))
    log.record(thread_id="t1", workflow="w", events=[
        {"event": "a", "ts": "2026-07-10T00:00:00+00:00"},
    ])

    result = log.verify()
    assert result.ok is True
    assert result.legacy == 1
    assert result.checked == 1

    lines = _lines(path)
    assert lines[1]["prev_hash"] == "0" * 64  # new chain starts at genesis


def test_verify_fails_on_unhashed_line_appended_after_hashing_began(tmp_path):
    path = tmp_path / "audit.log.jsonl"
    log = JsonlAuditLog(str(path))
    log.record(thread_id="t1", workflow="w", events=[
        {"event": "a", "ts": "2026-07-10T00:00:00+00:00"},
    ])
    with open(path, "a") as fh:
        fh.write(json.dumps({
            "thread_id": "t2", "workflow": "w", "event": "sneaky",
            "ts": "2026-07-10T00:00:01+00:00", "domain": None, "user_id": None,
        }) + "\n")

    result = log.verify()
    assert result.ok is False
    assert result.first_bad_line == 2
    assert "unhashed" in result.reason


def test_verify_fails_on_unparseable_line(tmp_path):
    path = tmp_path / "audit.log.jsonl"
    log = JsonlAuditLog(str(path))
    log.record(thread_id="t1", workflow="w", events=[
        {"event": "a", "ts": "2026-07-10T00:00:00+00:00"},
    ])
    with open(path, "a") as fh:
        fh.write("{not json\n")

    result = log.verify()
    assert result.ok is False
    assert result.first_bad_line == 2
    assert "invalid JSON" in result.reason


def test_query_unaffected_by_hash_fields(tmp_path):
    path = tmp_path / "audit.log.jsonl"
    log = JsonlAuditLog(str(path))
    log.record(
        thread_id="t1", workflow="w",
        events=[{"event": "a", "ts": "2026-07-10T00:00:00+00:00", "chars": 3}],
    )
    results = log.query()
    assert len(results) == 1
    assert results[0].fields == {"chars": 3}
