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
