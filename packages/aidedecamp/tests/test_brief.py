"""Morning brief tests — read-only, injected connector + client, no services."""

from __future__ import annotations

from datetime import datetime, timezone

from aidedecamp.brief import assemble_brief
from aidedecamp.connectors import McpWorkspaceConnector


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
                 "attendees": ["me@telus.com"]},
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

from aidedecamp.brief import find_quiet_threads  # noqa: E402
from aidedecamp.connectors.base import (  # noqa: E402
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
