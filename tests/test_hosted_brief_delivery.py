from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from attune.hosted.brief_delivery import (
    BriefDestination,
    BriefWork,
    HostedBriefExecutor,
    PostgresHostedBriefRepository,
    _event_from_summary,
    _parse_email_date,
    _parse_iso,
    _thread_from_summary,
)
from attune.hosted.repositories import HostedJob
from attune.hosted.secret_broker_client import CalendarEventSummary, GmailThreadSummary
from attune.hosted.tenant import TenantContext
from attune.orchestrator.importance import ImportanceTier, TierAssessment

TENANT = UUID("20000000-0000-4000-8000-000000000901")
JOB = UUID("20000000-0000-4000-8000-000000000902")
PRINCIPAL = UUID("20000000-0000-4000-8000-000000000903")
CONNECTOR = UUID("20000000-0000-4000-8000-000000000904")
GOOGLE_CHAT_DEST = UUID("20000000-0000-4000-8000-000000000905")
SLACK_DEST = UUID("20000000-0000-4000-8000-000000000906")
INTENT = UUID("20000000-0000-4000-8000-000000000907")
NOW = datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc)


def job(payload=None):
    return HostedJob(
        JOB, "channel.brief.deliver", "leased", "assistant.brief.deliver",
        payload or {"schema_version": 1, "principal_id": str(PRINCIPAL)},
        1, NOW, NOW,
    )


class FakeWork:
    """Models what the real repository's SQL already filters server-side:
    ``destinations`` only ever contains rows a preference-matching WHERE
    clause would have returned (brief_channels, not interaction_channels).
    An "interaction only" destination configured in a test never appears
    here at all -- exactly like the real query would omit it."""

    def __init__(self, destinations=()):
        self.destinations = list(destinations)
        self.proposed = []

    def resolve(self, context, value):
        assert context == TenantContext(TENANT) and value.id == JOB
        return BriefWork(PRINCIPAL, CONNECTOR)

    def list_brief_destinations(self, context, *, principal_id):
        assert context == TenantContext(TENANT) and principal_id == PRINCIPAL
        return tuple(self.destinations)

    def propose_delivery(self, context, *, job_id, destination_id, brief_text):
        self.proposed.append((job_id, destination_id, brief_text))


class FakeIntents:
    def __init__(self):
        self.calls = []

    def request(self, context, **kwargs):
        self.calls.append(kwargs)
        from types import SimpleNamespace

        return SimpleNamespace(id=INTENT, state="requested")


class FakeWorkspace:
    def __init__(self, threads=(), events=()):
        self.threads = threads
        self.events = events
        self.calls = []

    def google_gmail_threads(self, intent_id, **kwargs):
        self.calls.append(("gmail", intent_id, kwargs))
        return self.threads

    def google_calendar_events(self, intent_id, **kwargs):
        self.calls.append(("calendar", intent_id, kwargs))
        return self.events


class FakeReplies:
    def __init__(self):
        self.google_chat_calls = []
        self.slack_calls = []
        self.fail_provider = None

    def deliver_google_chat_brief(self, *, destination_id, job_id):
        self.google_chat_calls.append((destination_id, job_id))
        return self.fail_provider != "google_chat"

    def deliver_slack_brief(self, *, destination_id, job_id):
        self.slack_calls.append((destination_id, job_id))
        return self.fail_provider != "slack"


class FakeImportanceProfile:
    def __init__(self, tiers):
        self.tiers = tiers

    def assess(self, sender, *, now=None):
        tier = self.tiers.get(sender, ImportanceTier.NORMAL)
        return TierAssessment(tier, "fake", False)


class FakeAttentionStore:
    def __init__(self, items=()):
        self.items = items
        self.since_calls = []

    def recent(self, *, since=None, limit=None):
        self.since_calls.append(since)
        return list(self.items)


class FakeAudit:
    def __init__(self):
        self.calls = []

    def record(self, context, *, action, outcome, job_id, count):
        self.calls.append(
            {"context": context, "action": action, "outcome": outcome,
             "job_id": job_id, "count": count}
        )


def _thread(thread_id, subject, sender="high@example.com"):
    # snippet mirrors subject (not a shared literal) so two unrelated test
    # threads never accidentally topic-correlate (orchestrator/correlation.py
    # links on >=2 shared significant tokens across subject+snippet).
    return GmailThreadSummary(thread_id, subject, sender, "Date", subject)


def _event(event_id, summary):
    return CalendarEventSummary(event_id, summary, "start", "end", "", "confirmed")


def build_executor(
    *, work=None, workspace=None, replies=None, tiers=None, attention_items=(),
    audit=None,
):
    work = work or FakeWork()
    intents = FakeIntents()
    workspace = workspace or FakeWorkspace()
    replies = replies or FakeReplies()
    importance_profile = FakeImportanceProfile(tiers or {})
    attention_store = FakeAttentionStore(attention_items)
    executor = HostedBriefExecutor(
        work, intents, workspace, replies,
        lambda context, principal_id: importance_profile,
        lambda context, principal_id: attention_store,
        now=lambda: NOW,
        audit=audit,
    )
    return executor, work, intents, workspace, replies, attention_store


def test_bounded_reads_use_the_same_caps_as_the_conversational_brief_route():
    executor, work, intents, workspace, replies, _ = build_executor(
        work=FakeWork([BriefDestination(GOOGLE_CHAT_DEST, "google_chat")]),
    )
    executor(TenantContext(TENANT), job())
    gmail_call = next(call for call in workspace.calls if call[0] == "gmail")
    assert gmail_call[2] == {"query": "is:unread newer_than:1d", "limit": 10}
    calendar_call = next(call for call in workspace.calls if call[0] == "calendar")
    assert calendar_call[2]["limit"] == 25
    assert [c["capability"] for c in intents.calls] == [
        "google.gmail.threads.read", "google.calendar.events.read",
    ]


def test_spine_is_ordered_by_counterpart_importance_tier():
    """Deliverable 4: spine ordering by tier -- a HIGH-tier sender's thread
    ranks ahead of a NORMAL/LOW one, reusing brief.build_spine unmodified."""
    threads = [
        _thread("low", "Newsletter", sender="low@example.com"),
        _thread("high", "Contract renewal", sender="high@example.com"),
    ]
    executor, work, *_ = build_executor(
        work=FakeWork([BriefDestination(GOOGLE_CHAT_DEST, "google_chat")]),
        workspace=FakeWorkspace(threads=threads),
        tiers={"high@example.com": ImportanceTier.HIGH, "low@example.com": ImportanceTier.LOW},
    )
    executor(TenantContext(TENANT), job())
    text = work.proposed[0][2]
    assert text.index("Contract renewal") < text.index("Newsletter")


def test_empty_attention_store_is_fine():
    """Deliverable 4: empty-attention fine -- the spine still assembles from
    mail + calendar alone when attention_store.recent() returns nothing."""
    executor, work, *_ = build_executor(
        work=FakeWork([BriefDestination(GOOGLE_CHAT_DEST, "google_chat")]),
        workspace=FakeWorkspace(threads=[_thread("t1", "Hello")], events=[_event("e1", "Standup")]),
        attention_items=(),
    )
    executor(TenantContext(TENANT), job())
    text = work.proposed[0][2]
    assert "Hello" in text
    assert "Standup" in text
    assert "WHAT MATTERS NOW" in text


def test_bounded_sections_render_unread_and_events_counts():
    executor, work, *_ = build_executor(
        work=FakeWork([BriefDestination(GOOGLE_CHAT_DEST, "google_chat")]),
        workspace=FakeWorkspace(
            threads=[_thread("t1", "One"), _thread("t2", "Two")],
            events=[_event("e1", "Standup")],
        ),
    )
    executor(TenantContext(TENANT), job())
    text = work.proposed[0][2]
    assert "UNREAD MAIL (2):" in text
    assert "UPCOMING EVENTS (1):" in text


def test_delivery_fans_out_to_every_returned_brief_destination_only():
    """Deliverable 4: preference-respecting fan-out -- a brief-enabled
    destination gets delivered to; an interaction-only destination (never
    returned by list_brief_destinations, mirroring the real preference
    query) is never contacted at all."""
    executor, work, _, _, replies, _ = build_executor(
        work=FakeWork([
            BriefDestination(GOOGLE_CHAT_DEST, "google_chat"),
            BriefDestination(SLACK_DEST, "slack"),
        ]),
    )
    executor(TenantContext(TENANT), job())
    assert replies.google_chat_calls == [(GOOGLE_CHAT_DEST, JOB)]
    assert replies.slack_calls == [(SLACK_DEST, JOB)]
    assert {destination_id for _, destination_id, _ in work.proposed} == {
        GOOGLE_CHAT_DEST, SLACK_DEST,
    }
    # Both destinations receive the identical rendered text (one spine per
    # tenant run, not one per destination).
    assert len({text for _, _, text in work.proposed}) == 1


def test_no_brief_destinations_delivers_to_nobody_without_error():
    executor, work, _, _, replies, _ = build_executor(work=FakeWork([]))
    executor(TenantContext(TENANT), job())
    assert work.proposed == []
    assert replies.google_chat_calls == [] and replies.slack_calls == []


def test_delivery_failure_to_one_destination_raises():
    replies = FakeReplies()
    replies.fail_provider = "slack"
    executor, work, *_ = build_executor(
        work=FakeWork([
            BriefDestination(GOOGLE_CHAT_DEST, "google_chat"),
            BriefDestination(SLACK_DEST, "slack"),
        ]),
        replies=replies,
    )
    with pytest.raises(RuntimeError, match="not delivered"):
        executor(TenantContext(TENANT), job())


def test_content_free_audit_records_counts_only():
    audit = FakeAudit()
    executor, work, *_ = build_executor(
        work=FakeWork([BriefDestination(GOOGLE_CHAT_DEST, "google_chat")]),
        workspace=FakeWorkspace(threads=[_thread("t1", "Secret subject line")]),
        audit=audit,
    )
    executor(TenantContext(TENANT), job())
    actions = {call["action"]: call for call in audit.calls}
    assert set(actions) == {"brief.assemble", "brief.deliver"}
    assert actions["brief.deliver"]["count"] == 1
    assert all(isinstance(call["count"], int) for call in audit.calls)
    # Content-free: no rendered brief text, subject, or sender ever appears
    # in what's handed to the audit sink.
    for call in audit.calls:
        assert "Secret subject line" not in str(call)


def test_payload_helper_rejects_wrong_kind_or_capability():
    from attune.hosted.brief_delivery import _payload

    with pytest.raises(ValueError, match="does not match the fixed route"):
        _payload(HostedJob(
            JOB, "channel.web.converse", "leased", "assistant.brief.deliver",
            {"schema_version": 1, "principal_id": str(PRINCIPAL)}, 1, NOW, NOW,
        ))
    with pytest.raises(ValueError, match="does not match the contract"):
        _payload(HostedJob(
            JOB, "channel.brief.deliver", "leased", "assistant.brief.deliver",
            {"schema_version": 1}, 1, NOW, NOW,
        ))
    with pytest.raises(ValueError, match="principal reference is invalid"):
        _payload(HostedJob(
            JOB, "channel.brief.deliver", "leased", "assistant.brief.deliver",
            {"schema_version": 1, "principal_id": "not-a-uuid"}, 1, NOW, NOW,
        ))


def test_repository_rejects_a_resolved_principal_that_does_not_match_payload():
    """Defense in depth (mirrors the conversation executor's own payload
    round-trip check): even if the SQL join somehow returned a different
    principal than the job's own payload named, the repository refuses
    rather than trusting the row."""

    class StubCursor:
        def execute(self, *args, **kwargs):
            pass

        def fetchall(self):
            return [(UUID(int=999), CONNECTOR)]

        def close(self):
            pass

    class StubConnection:
        def cursor(self):
            return StubCursor()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    repo = PostgresHostedBriefRepository(lambda: StubConnection())
    with pytest.raises(RuntimeError, match="payload changed"):
        repo.resolve(TenantContext(TENANT), job())


def test_thread_adapter_maps_summary_fields_and_tolerates_bad_dates():
    summary = GmailThreadSummary("t1", "Subj", "from@example.com", "not-a-date", "snip")
    thread = _thread_from_summary(summary)
    assert thread.thread_id == "t1"
    assert thread.subject == "Subj"
    assert thread.from_addr == "from@example.com"
    assert thread.last_from_addr == "from@example.com"
    assert thread.body == ""
    assert thread.last_message_at is None


def test_thread_adapter_parses_a_real_rfc2822_date():
    summary = GmailThreadSummary(
        "t1", "Subj", "from@example.com", "Sun, 19 Jul 2026 08:00:00 +0000", "snip"
    )
    thread = _thread_from_summary(summary)
    assert thread.last_message_at == datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc)


def test_event_adapter_falls_back_to_now_on_bad_timestamps():
    summary = CalendarEventSummary("e1", "Standup", "garbage", "garbage", "", "confirmed")
    event = _event_from_summary(summary, now=NOW)
    assert event.start == NOW and event.end == NOW


def test_event_adapter_parses_iso_timestamps():
    summary = CalendarEventSummary(
        "e1", "Standup", "2026-07-19T09:00:00+00:00", "2026-07-19T09:30:00+00:00",
        "", "confirmed",
    )
    event = _event_from_summary(summary, now=NOW)
    assert event.start == datetime(2026, 7, 19, 9, 0, tzinfo=timezone.utc)


def test_parse_helpers_never_raise():
    assert _parse_email_date("") is None
    assert _parse_email_date(None) is None
    assert _parse_iso("") is None
    assert _parse_iso(None) is None
