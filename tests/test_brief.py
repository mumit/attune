"""Morning brief tests — read-only, injected connector + client, no services."""

from __future__ import annotations

from datetime import datetime, timezone

from attune.brief import assemble_brief
from attune.connectors import McpWorkspaceConnector


class FakeMcp:
    def __call__(self, server, tool, arguments):
        if tool == "search_threads":
            return {
                "threads": [
                    {"thread_id": "t1", "subject": "Contract", "snippet": "please review",
                     "from": "legal@acme.com", "body": "..."},
                    {"thread_id": "t2", "subject": "Lunch?", "snippet": "free friday",
                     "from": "sam@x.com", "body": "..."},
                ]
            }
        if tool == "list_events":
            return {"events": [
                {"event_id": "e1", "summary": "Standup",
                 "start": "2026-07-10T09:00:00+00:00", "end": "2026-07-10T09:15:00+00:00",
                 "attendees": ["me@example.com"]},
            ]}
        return {}


class FakeMsg:
    def __init__(self, c): self.message = type("M", (), {"content": c})


class FakeResp:
    def __init__(self, c): self.choices = [FakeMsg(c)]


class FakeClient:
    def __init__(self): self.calls = []
    def chat_completions_create(self, **kw):
        self.calls.append(kw)
        return FakeResp("2 unread (1 needs review), 1 event today.")


def test_brief_counts_and_summarizes():
    conn = McpWorkspaceConnector(FakeMcp())
    client = FakeClient()
    brief = assemble_brief(conn, client, now=datetime(2026, 7, 10, 7, tzinfo=timezone.utc))
    assert brief.unread_count == 2
    assert brief.event_count == 1
    assert "unread" in brief.summary


def test_brief_frames_mail_as_untrusted():
    conn = McpWorkspaceConnector(FakeMcp())
    client = FakeClient()
    assemble_brief(conn, client, now=datetime(2026, 7, 10, 7, tzinfo=timezone.utc))
    user_content = client.calls[0]["messages"][-1]["content"]
    assert "untrusted" in user_content.lower()


# ---------------------------------------------------------------------------
# v2 (prompt 07): timezone, meeting prep, quiet threads
# ---------------------------------------------------------------------------

from datetime import timedelta  # noqa: E402

from attune.brief import find_quiet_threads  # noqa: E402
from attune.connectors.base import (  # noqa: E402
    CalendarEvent,
    EmailThread,
    Provenance,
)


class FakeConnector:
    """Direct fake (no MCP indirection) with recordable list_threads calls."""

    def __init__(self, threads=None, events=None, sent=None):
        self._threads = threads or []
        self._events = events or []
        self._sent = sent or []
        self.event_windows: list[tuple] = []
        self.thread_queries: list[str] = []

    def list_threads(self, query="is:unread", *, max_results=20):
        self.thread_queries.append(query)
        if query == "in:sent":
            return self._sent
        return self._threads

    def list_events(self, *, time_min, time_max):
        self.event_windows.append((time_min, time_max))
        return self._events


def _thread(subject="Redline", last_from="me@example.com", last_at=None, **kw):
    return EmailThread(
        thread_id=kw.get("thread_id", "t1"),
        subject=subject,
        snippet=kw.get("snippet", "snippet text"),
        from_addr=kw.get("from_addr", "someone@x.com"),
        body="...",
        provenance=Provenance.FETCHED,
        last_from_addr=last_from,
        last_message_at=last_at,
    )


def _event(summary="Falcon sync", hour_utc=16, attendees=None):
    return CalendarEvent(
        event_id="e1",
        summary=summary,
        start=datetime(2026, 7, 10, hour_utc, 0, tzinfo=timezone.utc),
        end=datetime(2026, 7, 10, hour_utc + 1, 0, tzinfo=timezone.utc),
        attendees=attendees or [],
    )


class FakeStore:
    def __init__(self, results=None):
        self._results = results or []
        self.queries: list[str] = []

    def search(self, query, *, user_id, limit=8, min_score=None):
        self.queries.append(query)
        return self._results


def test_day_boundary_computed_in_local_timezone():
    """01:00 UTC on July 10 is still July 9 in Vancouver — the events window
    must cover the *local* day, not the UTC one (roadmap defect #7)."""
    conn = FakeConnector()
    assemble_brief(
        conn, FakeClient(),
        tz="America/Vancouver",
        now=datetime(2026, 7, 10, 1, 0, tzinfo=timezone.utc),
    )
    time_min, time_max = conn.event_windows[0]
    # local July 9 00:00 PDT == July 9 07:00 UTC
    assert time_min == datetime(2026, 7, 9, 7, 0, tzinfo=timezone.utc)
    assert time_max == datetime(2026, 7, 10, 7, 0, tzinfo=timezone.utc)


def test_event_times_rendered_in_local_timezone():
    conn = FakeConnector(events=[_event(hour_utc=16)])  # 16:00 UTC = 09:00 PDT
    client = FakeClient()
    assemble_brief(
        conn, client, tz="America/Vancouver",
        now=datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc),
    )
    content = client.calls[0]["messages"][-1]["content"]
    assert "09:00 Falcon sync" in content
    assert "times in America/Vancouver" in content


def test_meeting_prep_from_memory_and_related_thread():
    conn = FakeConnector(
        events=[_event(summary="Falcon sync", attendees=["priya@x.com"])],
        threads=[_thread(subject="Falcon timeline", snippet="latest numbers")],
    )
    store = FakeStore(results=[
        type("M", (), {"text": "Priya is the PM for Falcon"})()
    ])
    client = FakeClient()
    brief = assemble_brief(
        conn, client, store=store, user_id="u1",
        now=datetime(2026, 7, 10, 7, tzinfo=timezone.utc),
    )

    # memory searched with event context, related-thread query issued
    assert any("Falcon sync" in q for q in store.queries)
    assert any('"Falcon sync"' in q and "from:priya@x.com" in q
               for q in conn.thread_queries)
    # prep lines are inside the untrusted block fed to the one model call
    content = client.calls[0]["messages"][-1]["content"]
    assert "prep: Priya is the PM for Falcon" in content
    assert "prep: last thread: Falcon timeline" in content
    # and exposed structurally on the Brief
    assert brief.meetings[0].notes[0] == "Priya is the PM for Falcon"


def test_still_exactly_one_model_call_with_prep():
    conn = FakeConnector(events=[_event(), _event(summary="1:1")])
    client = FakeClient()
    assemble_brief(conn, client, store=FakeStore(),
                   now=datetime(2026, 7, 10, 7, tzinfo=timezone.utc))
    assert len(client.calls) == 1


def test_find_quiet_threads_age_and_authorship():
    now = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    old_mine = _thread(subject="Waiting", last_from="Me <me@example.com>",
                       last_at=now - timedelta(days=4))
    fresh_mine = _thread(subject="Fresh", last_from="me@example.com",
                         last_at=now - timedelta(days=1))
    old_theirs = _thread(subject="They replied", last_from="them@x.com",
                         last_at=now - timedelta(days=10))
    no_date = _thread(subject="No date", last_from="me@example.com", last_at=None)
    conn = FakeConnector(sent=[old_mine, fresh_mine, old_theirs, no_date])

    quiet = find_quiet_threads(conn, user_email="me@example.com", now=now)

    assert [t.subject for t in quiet] == ["Waiting"]


def test_quiet_threads_in_brief_only_with_user_email():
    now = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    conn = FakeConnector(
        sent=[_thread(subject="Waiting", last_from="me@example.com",
                      last_at=now - timedelta(days=5))]
    )
    client = FakeClient()
    brief = assemble_brief(conn, client, user_email="me@example.com", now=now)
    content = client.calls[0]["messages"][-1]["content"]
    assert "WAITING ON" in content
    assert "Waiting — you sent the last message 5d ago" in content
    assert brief.waiting_on[0].subject == "Waiting"

    # without user_email, no quiet section and no in:sent query
    conn2 = FakeConnector()
    client2 = FakeClient()
    assemble_brief(conn2, client2, now=now)
    assert "WAITING ON" not in client2.calls[0]["messages"][-1]["content"]
    assert "in:sent" not in conn2.thread_queries


# ---------------------------------------------------------------------------
# Phase 1 (docs/future-state.md, G11 partial): unread mail ordered by
# importance tier — HIGH first, then NORMAL, then LOW; LOW still shown.
# ---------------------------------------------------------------------------

from attune.orchestrator.importance import ImportanceTier, TierAssessment  # noqa: E402


class FakeImportanceProfile:
    def __init__(self, tiers: dict, raise_for: set | None = None):
        self._tiers = tiers
        self._raise_for = raise_for or set()

    def assess(self, sender, *, now=None):
        if sender in self._raise_for:
            raise RuntimeError("profile boom")
        return TierAssessment(
            self._tiers.get(sender, ImportanceTier.NORMAL), "test", False
        )


def test_unread_mail_ordered_high_then_normal_then_low():
    threads = [
        _thread(thread_id="t1", from_addr="low@x.com", subject="Newsletter"),
        _thread(thread_id="t2", from_addr="normal@x.com", subject="FYI note"),
        _thread(thread_id="t3", from_addr="high@x.com", subject="Client ask"),
        _thread(thread_id="t4", from_addr="high2@x.com", subject="VIP followup"),
    ]
    conn = FakeConnector(threads=threads)
    profile = FakeImportanceProfile({
        "low@x.com": ImportanceTier.LOW,
        "high@x.com": ImportanceTier.HIGH,
        "high2@x.com": ImportanceTier.HIGH,
    })
    client = FakeClient()
    assemble_brief(
        conn, client, now=datetime(2026, 7, 10, 7, tzinfo=timezone.utc),
        importance_profile=profile,
    )
    content = client.calls[0]["messages"][-1]["content"]

    # HIGH-tier senders first (stable within tier: arrival order preserved
    # between the two HIGH senders), then NORMAL, then LOW last.
    assert (
        content.index("Client ask")
        < content.index("VIP followup")
        < content.index("FYI note")
        < content.index("Newsletter")
    )
    # LOW is reordered to the back, never dropped — the brief is read-only
    # awareness of everything unread; triage decides what to draft, not this.
    assert "Newsletter" in content


def test_unread_mail_order_unchanged_without_a_profile():
    threads = [
        _thread(thread_id="t1", from_addr="a@x.com", subject="Alpha"),
        _thread(thread_id="t2", from_addr="b@x.com", subject="Beta"),
    ]
    conn = FakeConnector(threads=threads)
    client = FakeClient()
    assemble_brief(conn, client, now=datetime(2026, 7, 10, 7, tzinfo=timezone.utc))
    content = client.calls[0]["messages"][-1]["content"]
    assert content.index("Alpha") < content.index("Beta")


def test_unread_mail_order_falls_back_on_profile_failure():
    threads = [
        _thread(thread_id="t1", from_addr="a@x.com", subject="Alpha"),
        _thread(thread_id="t2", from_addr="b@x.com", subject="Beta"),
    ]
    conn = FakeConnector(threads=threads)
    profile = FakeImportanceProfile({}, raise_for={"a@x.com"})
    client = FakeClient()
    assemble_brief(
        conn, client, now=datetime(2026, 7, 10, 7, tzinfo=timezone.utc),
        importance_profile=profile,
    )
    content = client.calls[0]["messages"][-1]["content"]
    # a profile failure leaves the connector's own order untouched
    assert content.index("Alpha") < content.index("Beta")


# ---------------------------------------------------------------------------
# Phase 2 stage 2 (docs/future-state.md, G11/G3): the unified "what matters
# now" spine — deterministic cross-source correlation feeding one ranked,
# capped, cross-source list that leads the brief. The existing sections
# above are the drill-downs; every test above this line still exercises them
# unchanged (the spine is additive to the untrusted block, never a
# replacement for it).
# ---------------------------------------------------------------------------

from attune.memory.signals import ActionSignal  # noqa: E402
from attune.orchestrator.attention import AttentionItem  # noqa: E402
from attune.orchestrator.importance import JsonImportanceProfile  # noqa: E402
from attune.orchestrator.triage import Priority  # noqa: E402

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


def _attention_item(
    *, source="slack", channel_ref="C1", channel_name="proj-x",
    sender_ref="U1", sender_display="Someone", summary="hello",
    ts=NOW, priority=Priority.ROUTINE, mentions_principal=False,
    thread_ref=None,
):
    return AttentionItem(
        source=source, channel_ref=channel_ref, channel_name=channel_name,
        sender_ref=sender_ref, sender_display=sender_display, summary=summary,
        ts=ts, priority=priority, mentions_principal=mentions_principal,
        thread_ref=thread_ref,
    )


class _StubAttentionStore:
    """Mimics ``JsonAttentionStore.recent``'s ``since`` filtering (offline,
    no file I/O) so tests can check that ``assemble_brief`` actually passes
    the documented 24h cutoff rather than merely trusting a store that
    ignores it."""

    def __init__(self, items):
        self._items = list(items)

    def recent(self, *, since=None, limit=None):
        items = [it for it in self._items if since is None or it.ts >= since]
        items.sort(key=lambda it: it.ts, reverse=True)
        if limit is not None:
            items = items[:limit]
        return items


# ---------------------------------------------------------------------------
# THE PHASE 2 EXIT-CRITERION TEST
#
# docs/future-state.md, Phase 2 exit criteria: "a Slack message from the
# principal's manager about a topic that also has an unanswered email
# appears as one ranked item in the brief."
# ---------------------------------------------------------------------------


def test_phase2_exit_criterion_slack_and_mail_on_one_topic_are_one_spine_entry(tmp_path):
    """A HIGH-tier sender's Slack message about "the Q3 launch plan review"
    and an unread mail thread about "Q3 launch plan" from the same person
    correlate into ONE spine entry (not two), naming both sources, ranked
    first among non-urgent entries — the literal Phase 2 exit criterion."""
    profile = JsonImportanceProfile(str(tmp_path / "importance.json"))
    for _ in range(5):
        profile.record_signal("priya@x.com", ActionSignal.APPROVED, ts=NOW - timedelta(days=1))

    q3_mail = _thread(
        thread_id="t-q3", subject="Q3 launch plan", snippet="deck attached",
        from_addr="priya@x.com", last_from="priya@x.com",
        last_at=NOW - timedelta(minutes=10),
    )
    other_mail = _thread(
        thread_id="t-fac", subject="Facilities notice", snippet="garage closed",
        from_addr="facilities@corp.com", last_from="facilities@corp.com",
        last_at=NOW - timedelta(minutes=20),
    )
    q3_slack = _attention_item(
        source="slack", channel_name="proj-x", sender_ref="U999",
        sender_display="Priya Patel", summary="the Q3 launch plan review",
        ts=NOW - timedelta(minutes=5),
    )
    urgent_incident = _attention_item(
        source="slack", channel_name="incidents", sender_ref="U-ops",
        sender_display="Ops Bot", summary="Production server is down, need help now",
        ts=NOW - timedelta(minutes=2), priority=Priority.URGENT,
    )

    conn = FakeConnector(threads=[q3_mail, other_mail], events=[])
    store = _StubAttentionStore([q3_slack, urgent_incident])
    client = FakeClient()

    brief = assemble_brief(
        conn, client, now=NOW, importance_profile=profile, attention_store=store,
    )

    # Exactly one spine entry for the Q3 topic, not two.
    q3_entries = [line for line in brief.spine if "Q3 launch plan" in line]
    assert len(q3_entries) == 1
    entry = q3_entries[0]

    # The one entry names both correlated sources.
    assert "Q3 launch plan" in entry  # mail
    assert "Slack" in entry and "proj-x" in entry  # the correlated Slack message

    # Ranked first among non-urgent entries (the urgent incident outranks
    # everything per the sort key's first criterion, but the Q3 entry beats
    # the unrelated NORMAL-tier facilities notice).
    non_urgent = [line for line in brief.spine if "🔴" not in line]
    assert non_urgent[0] is entry


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def test_spine_urgent_attention_item_ranks_first():
    high_mail = _thread(
        thread_id="t1", subject="Contract redline", from_addr="vip@x.com",
        last_from="vip@x.com", last_at=NOW - timedelta(minutes=30),
    )
    urgent = _attention_item(
        summary="Production outage right now", priority=Priority.URGENT,
        ts=NOW - timedelta(minutes=1),
    )
    conn = FakeConnector(threads=[high_mail], events=[])
    store = _StubAttentionStore([urgent])
    profile = FakeImportanceProfile({"vip@x.com": ImportanceTier.HIGH})
    client = FakeClient()

    brief = assemble_brief(
        conn, client, now=NOW, importance_profile=profile, attention_store=store,
    )

    assert brief.spine[0].startswith("🔴")
    assert "Production outage" in brief.spine[0]


def test_spine_mentions_principal_ranks_above_higher_tier_without_mention():
    high_mail = _thread(
        thread_id="t1", subject="Contract redline", from_addr="vip@x.com",
        last_from="vip@x.com", last_at=NOW - timedelta(minutes=30),
    )
    mentioned = _attention_item(
        summary="quick question for you", mentions_principal=True,
        ts=NOW - timedelta(minutes=1),
    )
    conn = FakeConnector(threads=[high_mail], events=[])
    store = _StubAttentionStore([mentioned])
    profile = FakeImportanceProfile({"vip@x.com": ImportanceTier.HIGH})
    client = FakeClient()

    brief = assemble_brief(
        conn, client, now=NOW, importance_profile=profile, attention_store=store,
    )

    assert "quick question for you" in brief.spine[0]
    assert brief.spine[0].startswith("🔴")


def test_spine_multi_source_group_ranks_above_single_source():
    linked_mail = _thread(
        thread_id="t1", subject="Q3 launch plan", snippet="", from_addr="a@x.com",
        last_from="a@x.com", last_at=NOW - timedelta(minutes=15),
    )
    linked_slack = _attention_item(
        summary="Q3 launch plan review thread", sender_ref="b@x.com",
        ts=NOW - timedelta(minutes=10),
    )
    solo_mail = _thread(
        thread_id="t2", subject="Standalone unrelated topic", snippet="",
        from_addr="c@x.com",
        last_from="c@x.com", last_at=NOW - timedelta(minutes=5),
    )
    conn = FakeConnector(threads=[linked_mail, solo_mail], events=[])
    store = _StubAttentionStore([linked_slack])
    client = FakeClient()

    brief = assemble_brief(conn, client, now=NOW, attention_store=store)

    assert "Q3 launch plan" in brief.spine[0]
    assert "Standalone unrelated topic" in brief.spine[1]


def test_spine_ranked_by_importance_tier():
    threads = [
        _thread(thread_id="t1", from_addr="low@x.com", subject="Newsletter blast",
                snippet="", last_from="low@x.com", last_at=NOW - timedelta(hours=1)),
        _thread(thread_id="t2", from_addr="normal@x.com", subject="FYI note here",
                snippet="", last_from="normal@x.com", last_at=NOW - timedelta(hours=2)),
        _thread(thread_id="t3", from_addr="high@x.com", subject="Client ask now",
                snippet="", last_from="high@x.com", last_at=NOW - timedelta(hours=3)),
    ]
    conn = FakeConnector(threads=threads, events=[])
    profile = FakeImportanceProfile({
        "low@x.com": ImportanceTier.LOW, "high@x.com": ImportanceTier.HIGH,
    })
    client = FakeClient()

    brief = assemble_brief(conn, client, now=NOW, importance_profile=profile)

    assert "Client ask now" in brief.spine[0]
    assert "FYI note here" in brief.spine[1]
    assert "Newsletter blast" in brief.spine[2]


_DISTINCT_WORDS = [
    "falcon", "heron", "otter", "walrus", "badger", "condor", "marlin",
    "toucan", "beetle", "gibbon", "lizard", "weasel",
]


def test_spine_capped_at_ten_entries():
    # Each subject is a single word, all different, sharing no significant
    # token with any other — nothing here should correlate into one group.
    threads = [
        _thread(
            thread_id=f"t{i}", from_addr=f"sender{i}@x.com",
            subject=_DISTINCT_WORDS[i].capitalize(), snippet="",
            last_from=f"sender{i}@x.com", last_at=NOW - timedelta(minutes=i),
        )
        for i in range(12)
    ]
    conn = FakeConnector(threads=threads, events=[])
    client = FakeClient()

    brief = assemble_brief(conn, client, now=NOW)

    assert brief.unread_count == 12  # nothing dropped from the drill-down
    assert len(brief.spine) == 10  # SPINE_CAP


def test_attention_items_older_than_24h_excluded_from_spine():
    stale = _attention_item(
        summary="Stale ancient topic mention", ts=NOW - timedelta(hours=25),
    )
    fresh = _attention_item(
        summary="Fresh recent topic mention", ts=NOW - timedelta(hours=1),
    )
    conn = FakeConnector(threads=[], events=[])
    store = _StubAttentionStore([stale, fresh])
    client = FakeClient()

    brief = assemble_brief(conn, client, now=NOW, attention_store=store)

    joined = "\n".join(brief.spine)
    assert "Fresh recent topic mention" in joined
    assert "Stale ancient topic mention" not in joined


def test_spine_store_less_backward_compatible():
    """No ``attention_store`` (the CLI plain-preview path, by design): the
    spine is built from mail + calendar alone, and every legacy section
    (asserted by the tests above this section, all still passing unchanged)
    keeps its exact pre-stage-2 rendering."""
    conn = FakeConnector(
        threads=[_thread(subject="Contract redline", from_addr="legal@acme.com")],
        events=[_event(summary="Falcon sync")],
    )
    client = FakeClient()

    brief = assemble_brief(conn, client, now=NOW)

    assert brief.spine  # still leads with something sensible
    joined = "\n".join(brief.spine)
    assert "Contract redline" in joined or "Falcon sync" in joined


# ---------------------------------------------------------------------------
# Phase 3 stage 3 (docs/future-state.md Phase 3, item 4; G11): "what changed
# since yesterday" (Deliverable A), waiting-on ages/ordering (Deliverable B),
# and inline pending-approval pointers (Deliverable C).
# ---------------------------------------------------------------------------

from attune.brief import BriefSnapshot, JsonBriefSnapshot  # noqa: E402


class _RecordingPending:
    """Minimal PendingApprovals-shaped fake keyed by source_ref, for pointer
    and tally assertions."""

    def __init__(self, source_refs=()):
        self._refs = set(source_refs)

    def get_pending_for_source(self, source_ref):
        return object() if source_ref in self._refs else None

    def pending(self):
        return [object() for _ in self._refs]


class _RaisingPending:
    """A pending registry whose every method raises — lookups/tallies must
    degrade to "no pointer"/"no tally", never break the brief."""

    def get_pending_for_source(self, source_ref):
        raise RuntimeError("pending boom")

    def pending(self):
        raise RuntimeError("pending boom")


# --- Deliverable A: the "since yesterday" snapshot ------------------------


def test_first_run_has_no_since_yesterday_but_writes_a_snapshot(tmp_path):
    store = JsonBriefSnapshot(str(tmp_path / "snap.json"))
    conn = FakeConnector(
        threads=[_thread(thread_id="t1", subject="Contract redline")],
        events=[_event(summary="Falcon sync")],
    )
    client = FakeClient()

    brief = assemble_brief(conn, client, now=NOW, snapshot_store=store)

    assert brief.since_yesterday == []
    written = store.load()
    assert written is not None
    assert written.unread == [{"id": "t1", "text": "Contract redline"}]
    assert written.events[0]["id"] == "e1"
    assert written.ts == NOW


def test_since_yesterday_reports_new_and_resolved_and_new_events(tmp_path):
    store = JsonBriefSnapshot(str(tmp_path / "snap.json"))
    store.save(BriefSnapshot(
        unread=[{"id": "old1", "text": "Stale thread"}],
        events=[{"id": "old-e1", "text": "Yesterday's meeting"}],
        quiet_thread_ids=["q1"],
        ts=NOW - timedelta(hours=20),
    ))
    conn = FakeConnector(
        threads=[_thread(thread_id="new1", subject="Brand new thread")],
        events=[_event(summary="Today's meeting")],
    )
    client = FakeClient()

    brief = assemble_brief(conn, client, now=NOW, snapshot_store=store)

    joined = "\n".join(brief.since_yesterday)
    assert "New unread (1): Brand new thread" in joined
    assert "Resolved (1): Stale thread" in joined
    assert "New events (1): Today's meeting" in joined
    assert "Still waiting: 0 (-1 vs yesterday)" in joined
    # rendered right after the spine in the untrusted block fed to the model
    content = client.calls[0]["messages"][-1]["content"]
    assert content.index("SINCE YESTERDAY") > content.index("WHAT MATTERS NOW")
    assert content.index("UNREAD MAIL") > content.index("SINCE YESTERDAY")


def test_since_yesterday_list_capped_with_more_suffix(tmp_path):
    store = JsonBriefSnapshot(str(tmp_path / "snap.json"))
    store.save(BriefSnapshot(unread=[], events=[], quiet_thread_ids=[], ts=NOW))
    threads = [
        _thread(thread_id=f"t{i}", subject=f"Subject {i}") for i in range(7)
    ]
    conn = FakeConnector(threads=threads)
    client = FakeClient()

    brief = assemble_brief(conn, client, now=NOW, snapshot_store=store)

    line = next(
        candidate for candidate in brief.since_yesterday
        if candidate.startswith("New unread")
    )
    assert line.startswith("New unread (7): ")
    assert line.endswith(", +2 more")


def test_stale_snapshot_older_than_48h_is_ignored(tmp_path):
    store = JsonBriefSnapshot(str(tmp_path / "snap.json"))
    store.save(BriefSnapshot(
        unread=[{"id": "old1", "text": "Stale thread"}],
        events=[], quiet_thread_ids=[],
        ts=NOW - timedelta(hours=48),  # exactly at the boundary: stale
    ))
    conn = FakeConnector(threads=[_thread(thread_id="new1")])
    client = FakeClient()

    brief = assemble_brief(conn, client, now=NOW, snapshot_store=store)

    assert brief.since_yesterday == []


def test_snapshot_read_failure_never_breaks_the_brief():
    class _RaisingSnapshotStore:
        def load(self):
            raise RuntimeError("disk boom")

        def save(self, snapshot):
            raise RuntimeError("disk boom")

    conn = FakeConnector(threads=[_thread()])
    client = FakeClient()

    brief = assemble_brief(conn, client, now=NOW, snapshot_store=_RaisingSnapshotStore())

    assert brief.since_yesterday == []
    assert brief.summary


def test_no_snapshot_store_means_no_since_yesterday_and_no_file(tmp_path):
    """The CLI's plain preview path (no ``snapshot_store``): no section, and
    — since nothing was even constructed — no state file as a side effect."""
    conn = FakeConnector(threads=[_thread()])
    client = FakeClient()

    brief = assemble_brief(conn, client, now=NOW)

    assert brief.since_yesterday == []
    assert not (tmp_path / "snap.json").exists()


# --- Deliverable B: waiting-on ordered by tier, then age ------------------


def test_waiting_on_ordered_by_tier_then_age_longest_first():
    high_recent = _thread(
        thread_id="w-high", subject="High recent", from_addr="vip@x.com",
        last_from="me@example.com", last_at=NOW - timedelta(days=1),
    )
    high_old = _thread(
        thread_id="w-high-old", subject="High old", from_addr="vip@x.com",
        last_from="me@example.com", last_at=NOW - timedelta(days=6),
    )
    normal_old = _thread(
        thread_id="w-normal", subject="Normal old", from_addr="normal@x.com",
        last_from="me@example.com", last_at=NOW - timedelta(days=10),
    )
    conn = FakeConnector(sent=[normal_old, high_recent, high_old])
    profile = FakeImportanceProfile({"vip@x.com": ImportanceTier.HIGH})
    client = FakeClient()

    brief = assemble_brief(
        conn, client, user_email="me@example.com", now=NOW,
        importance_profile=profile, quiet_min_age_days=0,
    )

    assert [t.subject for t in brief.waiting_on] == [
        "High old", "High recent", "Normal old",
    ]


def test_waiting_on_order_unchanged_without_a_profile_ages_only():
    newer = _thread(
        thread_id="w1", subject="Newer", last_from="me@example.com",
        last_at=NOW - timedelta(days=3),
    )
    older = _thread(
        thread_id="w2", subject="Older", last_from="me@example.com",
        last_at=NOW - timedelta(days=8),
    )
    conn = FakeConnector(sent=[newer, older])
    client = FakeClient()

    brief = assemble_brief(conn, client, user_email="me@example.com", now=NOW)

    assert [t.subject for t in brief.waiting_on] == ["Older", "Newer"]


# --- Deliverable C: inline pending-approval pointers + bottom-of-spine tally


def test_spine_entry_gets_pointer_when_underlying_thread_is_pending():
    mail = _thread(thread_id="t-pending", subject="Contract redline", snippet="")
    conn = FakeConnector(threads=[mail], events=[])
    pending = _RecordingPending(source_refs={"t-pending"})
    client = FakeClient()

    brief = assemble_brief(conn, client, now=NOW, pending=pending)

    assert any("Contract redline" in line and "approval card pending" in line
               for line in brief.spine)


def test_unread_mail_line_gets_pointer_for_pending_thread():
    mail = _thread(thread_id="t-pending", subject="Newsletter", snippet="")
    conn = FakeConnector(threads=[mail])
    pending = _RecordingPending(source_refs={"t-pending"})
    client = FakeClient()

    assemble_brief(conn, client, now=NOW, pending=pending)
    content = client.calls[0]["messages"][-1]["content"]
    mail_line = next(
        line for line in content.splitlines() if line.startswith("- from")
    )

    assert "Newsletter" in mail_line
    assert mail_line.endswith("approval card pending")


def test_event_line_gets_pointer_for_pending_event():
    event = _event(summary="1:1 with boss")
    conn = FakeConnector(events=[event])
    pending = _RecordingPending(source_refs={"e1"})
    client = FakeClient()

    assemble_brief(conn, client, now=NOW, pending=pending)
    content = client.calls[0]["messages"][-1]["content"]

    assert "1:1 with boss → approval card pending" in content


def test_waiting_on_line_gets_pointer_for_pending_followup():
    quiet = _thread(
        thread_id="t-quiet", subject="Waiting", last_from="me@example.com",
        last_at=NOW - timedelta(days=5),
    )
    conn = FakeConnector(sent=[quiet])
    pending = _RecordingPending(source_refs={"t-quiet"})
    client = FakeClient()

    assemble_brief(conn, client, user_email="me@example.com", now=NOW, pending=pending)
    content = client.calls[0]["messages"][-1]["content"]
    waiting_line = next(
        line for line in content.splitlines() if line.startswith("- Waiting")
    )

    assert waiting_line.endswith("approval card pending")


def test_pending_tally_rendered_when_cards_pending_generic_fallback():
    conn = FakeConnector(threads=[_thread()])
    pending = _RecordingPending(source_refs={"t1", "t2"})
    client = FakeClient()

    brief = assemble_brief(conn, client, now=NOW, pending=pending)

    assert brief.pending_tally == "2 proposals awaiting your decision in your approval channel"
    assert brief.pending_tally in "\n".join(client.calls[0]["messages"][-1]["content"].splitlines())


def test_pending_tally_uses_channel_name_when_supplied():
    conn = FakeConnector(threads=[_thread()])
    pending = _RecordingPending(source_refs={"t1"})
    client = FakeClient()

    brief = assemble_brief(
        conn, client, now=NOW, pending=pending, approval_channel_name="#approvals",
    )

    assert brief.pending_tally == "1 proposal awaiting your decision in #approvals"


def test_no_pending_registry_means_no_pointers_and_no_tally():
    conn = FakeConnector(threads=[_thread(subject="Contract redline")])
    client = FakeClient()

    brief = assemble_brief(conn, client, now=NOW)

    assert brief.pending_tally is None
    assert all("approval card pending" not in line for line in brief.spine)


def test_pending_lookup_failure_never_breaks_the_brief():
    conn = FakeConnector(threads=[_thread(subject="Contract redline")])
    client = FakeClient()

    brief = assemble_brief(conn, client, now=NOW, pending=_RaisingPending())

    assert brief.pending_tally is None
    assert brief.summary
