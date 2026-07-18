"""Tests for orchestrator/correlation.py — deterministic cross-source
correlation (Phase 2 stage 2, ``docs/future-state.md``; gap G3). All pure:
no I/O, no model calls, no injected fakes needed beyond plain dataclasses.
"""

from __future__ import annotations

from datetime import datetime, timezone

from attune.connectors.base import CalendarEvent, EmailThread, Provenance
from attune.orchestrator.attention import AttentionItem
from attune.orchestrator.correlation import (
    CorrelatableItem,
    correlate,
    from_attention_item,
    from_calendar_event,
    from_mail_thread,
)
from attune.orchestrator.triage import Priority

T0 = datetime(2026, 7, 18, 9, 0, tzinfo=timezone.utc)


def _item(*, kind="mail", ref="r1", text="", participants=(), ts=T0, origin=None):
    return CorrelatableItem(
        kind=kind, ref=ref, text=text,
        participants=frozenset(participants), ts=ts, origin=origin,
    )


# ---------------------------------------------------------------------------
# Linking rules
# ---------------------------------------------------------------------------


def test_linked_by_shared_participant_email():
    mail = _item(kind="mail", ref="m1", text="Contract redline", ts=T0,
                 participants=("sam@x.com",))
    slack = _item(kind="source", ref="s1", text="unrelated ping",
                  ts=T0.replace(minute=5), participants=("sam@x.com",))

    groups = correlate([mail, slack])

    assert len(groups) == 1
    assert {it.ref for it in groups[0]} == {"m1", "s1"}


def test_linked_by_two_shared_significant_topic_tokens():
    mail = _item(kind="mail", ref="m1", text="Q3 launch plan",
                 participants=("alice@x.com",), ts=T0)
    slack = _item(kind="source", ref="s1", text="the Q3 launch plan review",
                  participants=("bob@x.com",), ts=T0.replace(minute=5))

    groups = correlate([mail, slack])

    assert len(groups) == 1
    assert {it.ref for it in groups[0]} == {"m1", "s1"}


def test_not_linked_on_a_single_shared_short_or_common_token():
    """The only word the two texts share is "Q3" — 2 characters, filtered by
    the minimum-token-length rule before any comparison happens — so there is
    zero significant-token overlap, and distinct participants give nothing to
    link on either."""
    a = _item(kind="mail", ref="m1", text="Q3 numbers due Friday",
              participants=("alice@x.com",), ts=T0)
    b = _item(kind="source", ref="s1", text="Q3 headcount review",
              participants=("bob@x.com",), ts=T0.replace(minute=5))

    groups = correlate([a, b])

    assert len(groups) == 2


def test_not_linked_on_shared_single_first_name():
    """A single-word display name ("John") is dropped entirely by the
    participant normalizer (:func:`_participant_tokens`) — it never becomes a
    token in the first place — so two otherwise unrelated messages whose
    senders happen to share a common first name must not merge. Built
    through the real ``from_attention_item`` normalizer, not a hand-built
    ``CorrelatableItem``, so the normalizer is actually exercised."""
    a = from_attention_item(AttentionItem(
        source="slack", channel_ref="C1", channel_name="general",
        sender_ref="U1", sender_display="John", summary="Renewal paperwork",
        ts=T0, priority=Priority.ROUTINE, mentions_principal=False,
        thread_ref=None,
    ))
    b = from_attention_item(AttentionItem(
        source="slack", channel_ref="C2", channel_name="social",
        sender_ref="U2", sender_display="John", summary="Lunch plans",
        ts=T0.replace(minute=5), priority=Priority.ROUTINE,
        mentions_principal=False, thread_ref=None,
    ))

    assert a.participants == frozenset()  # sanity: "John" alone is dropped
    groups = correlate([a, b])

    assert len(groups) == 2


def test_not_linked_on_different_multiword_names_sharing_a_word():
    """"John Smith" and "John Doe" share the word "John" but are different
    full names — exact multi-word match is required, not a word-level
    intersection."""
    a = _item(kind="mail", ref="m1", text="Renewal paperwork",
              participants=("john smith",), ts=T0)
    b = _item(kind="source", ref="s1", text="Lunch plans",
              participants=("john doe",), ts=T0.replace(minute=5))

    groups = correlate([a, b])

    assert len(groups) == 2


def test_three_way_transitive_grouping():
    """A links to B by topic, B links to C by participant email; A and C
    share neither — the union-find still merges all three into one group."""
    a = _item(kind="mail", ref="a", text="Q3 launch plan kickoff",
              participants=("alice@x.com",), ts=T0)
    b = _item(kind="source", ref="b", text="Q3 launch plan follow-up",
              participants=("carol@x.com",), ts=T0.replace(minute=5))
    c = _item(kind="calendar", ref="c", text="Unrelated standup",
              participants=("carol@x.com",), ts=T0.replace(minute=10))

    groups = correlate([a, b, c])

    assert len(groups) == 1
    assert {it.ref for it in groups[0]} == {"a", "b", "c"}


def test_empty_input_returns_no_groups():
    assert correlate([]) == []


def test_singleton_item_is_its_own_group():
    solo = _item(ref="only")
    assert correlate([solo]) == [[solo]]


def test_groups_sorted_by_earliest_timestamp_stable():
    early = _item(ref="early", ts=T0, participants=("early@x.com",))
    middle = _item(ref="middle", ts=T0.replace(hour=10), participants=("mid@x.com",))
    late = _item(ref="late", ts=T0.replace(hour=12), participants=("late@x.com",))

    groups = correlate([late, early, middle])

    assert [g[0].ref for g in groups] == ["early", "middle", "late"]


def test_items_within_a_group_sorted_by_timestamp():
    older = _item(kind="mail", ref="older", text="Q3 launch plan",
                  ts=T0, participants=("x@x.com",))
    newer = _item(kind="source", ref="newer", text="Q3 launch plan check-in",
                  ts=T0.replace(hour=10), participants=("y@x.com",))

    groups = correlate([newer, older])

    assert len(groups) == 1
    assert [it.ref for it in groups[0]] == ["older", "newer"]


# ---------------------------------------------------------------------------
# Builders — from the real dataclass shapes brief.py already works with.
# ---------------------------------------------------------------------------


def test_from_mail_thread_builds_participants_and_text():
    thread = EmailThread(
        thread_id="t1", subject="Q3 launch plan", snippet="please review",
        from_addr="Priya Patel <priya@x.com>", body="...",
        provenance=Provenance.FETCHED,
        last_from_addr="priya@x.com",
        last_message_at=T0,
    )

    item = from_mail_thread(thread)

    assert item.kind == "mail"
    assert item.ref == "t1"
    assert "Q3 launch plan" in item.text
    assert "priya@x.com" in item.participants
    assert "priya patel" in item.participants
    assert item.ts == T0
    assert item.origin is thread


def test_from_mail_thread_falls_back_to_now_when_no_timestamp():
    thread = EmailThread(
        thread_id="t1", subject="No dates here", snippet="", from_addr="a@x.com",
        body="...",
    )
    item = from_mail_thread(thread, now=T0)
    assert item.ts == T0


def test_from_attention_item_builds_participants_and_text():
    att = AttentionItem(
        source="slack", channel_ref="C1", channel_name="proj-x",
        sender_ref="U123", sender_display="Priya Patel",
        summary="the Q3 launch plan review", ts=T0,
        priority=Priority.ROUTINE, mentions_principal=False, thread_ref=None,
    )

    item = from_attention_item(att)

    assert item.kind == "source"
    assert "priya patel" in item.participants
    assert "the Q3 launch plan review" in item.text
    assert item.ts == T0
    assert item.origin is att


def test_from_calendar_event_builds_participants_and_text():
    event = CalendarEvent(
        event_id="e1", summary="Falcon sync", start=T0,
        end=T0.replace(hour=10),
        attendees=["Priya Patel <priya@x.com>", "sam@x.com"],
    )

    item = from_calendar_event(event)

    assert item.kind == "calendar"
    assert item.ref == "e1"
    assert item.text == "Falcon sync"
    assert "priya@x.com" in item.participants
    assert "sam@x.com" in item.participants
    assert item.ts == T0
    assert item.origin is event


def test_builders_correlate_end_to_end():
    """A same-person mail thread and Slack attention item, built through the
    real builders, correlate into one group — the exit-criterion behavior at
    the correlation-module level (the full brief-level exit-criterion test
    lives in test_brief.py)."""
    thread = EmailThread(
        thread_id="t1", subject="Q3 launch plan", snippet="deck attached",
        from_addr="Priya Patel <priya@x.com>", body="...",
        last_message_at=T0,
    )
    att = AttentionItem(
        source="slack", channel_ref="C1", channel_name="proj-x",
        sender_ref="U123", sender_display="Priya Patel",
        summary="the Q3 launch plan review", ts=T0.replace(minute=30),
        priority=Priority.ROUTINE, mentions_principal=False, thread_ref=None,
    )

    groups = correlate([from_mail_thread(thread), from_attention_item(att)])

    assert len(groups) == 1
    assert {it.kind for it in groups[0]} == {"mail", "source"}
