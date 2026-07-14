"""Tests for conversation.py — the ephemeral per-(channel,user) Q&A window
(design 2.1 working memory, roadmap prompt 04). All offline: file-backed log
in tmp_path, injected clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from attune.conversation import JsonConversationLog

T0 = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)


def _log(tmp_path, **kw):
    return JsonConversationLog(str(tmp_path / "conv.json"), **kw)


def test_append_and_recent_round_trip(tmp_path):
    log = _log(tmp_path)
    log.append(channel="slack", user_id="U1", role="user", content="hi", now=T0)
    log.append(
        channel="slack", user_id="U1", role="assistant", content="hello!", now=T0
    )

    turns = log.recent(channel="slack", user_id="U1", now=T0)
    assert turns == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello!"},
    ]


def test_windows_isolated_per_channel_and_user(tmp_path):
    log = _log(tmp_path)
    log.append(channel="slack", user_id="U1", role="user", content="a", now=T0)
    log.append(channel="chat", user_id="U1", role="user", content="b", now=T0)
    log.append(channel="slack", user_id="U2", role="user", content="c", now=T0)

    assert [t["content"] for t in log.recent(channel="slack", user_id="U1", now=T0)] == ["a"]
    assert [t["content"] for t in log.recent(channel="chat", user_id="U1", now=T0)] == ["b"]
    assert [t["content"] for t in log.recent(channel="slack", user_id="U2", now=T0)] == ["c"]


def test_ttl_evicts_old_turns_on_read(tmp_path):
    """A window that sat on disk past the TTL comes back empty even though
    nothing rewrote the file."""
    log = _log(tmp_path, ttl_minutes=120)
    log.append(channel="slack", user_id="U1", role="user", content="old", now=T0)

    fresh = log.recent(channel="slack", user_id="U1", now=T0 + timedelta(minutes=119))
    stale = log.recent(channel="slack", user_id="U1", now=T0 + timedelta(minutes=121))
    assert [t["content"] for t in fresh] == ["old"]
    assert stale == []


def test_window_capped_at_max_turns(tmp_path):
    log = _log(tmp_path, max_turns=4)
    for i in range(6):
        log.append(
            channel="slack", user_id="U1", role="user", content=f"m{i}", now=T0
        )

    turns = log.recent(channel="slack", user_id="U1", now=T0)
    assert [t["content"] for t in turns] == ["m2", "m3", "m4", "m5"]


def test_survives_reload(tmp_path):
    path = str(tmp_path / "conv.json")
    JsonConversationLog(path).append(
        channel="chat", user_id="U1", role="user", content="persisted", now=T0
    )
    turns = JsonConversationLog(path).recent(channel="chat", user_id="U1", now=T0)
    assert [t["content"] for t in turns] == ["persisted"]
