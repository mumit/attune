"""Tests for orchestrator/attention.py — the bounded attention store (Phase 2
stage 1, docs/future-state.md; gaps G1/G3). All offline: file-backed store
in tmp_path, injected clocks via explicit timestamps.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from attune.orchestrator.attention import (
    MAX_ITEMS,
    RETENTION_DAYS,
    AttentionItem,
    JsonAttentionStore,
)
from attune.orchestrator.triage import Priority

T0 = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


def _item(*, ts=T0, priority=Priority.ROUTINE, channel_ref="C1", sender_ref="U1", mentions=False):
    return AttentionItem(
        source="slack",
        channel_ref=channel_ref,
        channel_name=channel_ref,
        sender_ref=sender_ref,
        sender_display=sender_ref,
        summary="hello",
        ts=ts,
        priority=priority,
        mentions_principal=mentions,
        thread_ref=None,
    )


def _store(tmp_path):
    return JsonAttentionStore(str(tmp_path / "attention.json"))


def test_add_and_recent_round_trips(tmp_path):
    store = _store(tmp_path)
    store.add(_item(ts=T0, sender_ref="alice"))
    store.add(_item(ts=T0 + timedelta(minutes=1), sender_ref="bob"))

    recent = store.recent()
    assert [it.sender_ref for it in recent] == ["bob", "alice"]  # newest first
    assert recent[0].priority == Priority.ROUTINE
    assert recent[0].mentions_principal is False


def test_recent_since_filters_older_items(tmp_path):
    store = _store(tmp_path)
    store.add(_item(ts=T0, sender_ref="old"))
    store.add(_item(ts=T0 + timedelta(hours=2), sender_ref="new"))

    recent = store.recent(since=T0 + timedelta(hours=1))
    assert [it.sender_ref for it in recent] == ["new"]


def test_recent_limit_caps_results(tmp_path):
    store = _store(tmp_path)
    for i in range(5):
        store.add(_item(ts=T0 + timedelta(minutes=i), sender_ref=f"u{i}"))

    recent = store.recent(limit=2)
    assert len(recent) == 2
    assert recent[0].sender_ref == "u4"  # newest first
    assert recent[1].sender_ref == "u3"


def test_retention_window_drops_items_older_than_7_days(tmp_path):
    store = _store(tmp_path)
    old_ts = T0 - timedelta(days=RETENTION_DAYS + 1)
    store.add(_item(ts=old_ts, sender_ref="stale"))
    # A later add triggers the retention sweep on write.
    store.add(_item(ts=T0, sender_ref="fresh"))

    recent = store.recent()
    assert [it.sender_ref for it in recent] == ["fresh"]


def test_item_cap_keeps_most_recent(tmp_path):
    store = _store(tmp_path)
    base = T0
    for i in range(MAX_ITEMS + 10):
        store.add(_item(ts=base + timedelta(minutes=i), sender_ref=f"u{i}"))

    recent = store.recent(limit=None)
    assert len(recent) == MAX_ITEMS
    # The oldest 10 were dropped; the newest MAX_ITEMS survive.
    senders = {it.sender_ref for it in recent}
    assert "u0" not in senders
    assert f"u{MAX_ITEMS + 9}" in senders


def test_persists_across_store_instances(tmp_path):
    path = str(tmp_path / "attention.json")
    JsonAttentionStore(path).add(_item(sender_ref="alice"))

    reopened = JsonAttentionStore(path)
    assert [it.sender_ref for it in reopened.recent()] == ["alice"]


def test_empty_store_returns_no_items(tmp_path):
    store = _store(tmp_path)
    assert store.recent() == []


def test_urgent_priority_round_trips(tmp_path):
    store = _store(tmp_path)
    store.add(_item(priority=Priority.URGENT, mentions=True))
    item = store.recent()[0]
    assert item.priority == Priority.URGENT
    assert item.mentions_principal is True


def test_concurrent_processes_serialize_via_fslock(tmp_path):
    """Two store instances writing to the same path must not clobber each
    other's entries — the fslock.locked critical section (finding F2's
    pattern, mirrored from pending.py/importance.py) is what guarantees
    this, not just the in-process threading.RLock."""
    path = str(tmp_path / "attention.json")
    store_a = JsonAttentionStore(path)
    store_b = JsonAttentionStore(path)

    store_a.add(_item(sender_ref="from-a"))
    store_b.add(_item(sender_ref="from-b", ts=T0 + timedelta(minutes=1)))

    senders = {it.sender_ref for it in JsonAttentionStore(path).recent()}
    assert senders == {"from-a", "from-b"}
