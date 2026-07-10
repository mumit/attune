"""Managed-MCP implementation of :class:`WorkspaceConnector` (design 4.3).

Targets Google's managed Workspace MCP servers (gmail, calendar, chat, people,
drive), which authenticate per-request with a Google OAuth bearer token
(ya29.*) and expose tools like ``gmail.search_threads``, ``gmail.get_thread``,
``gmail.create_draft``, and message labeling.

Two things worth stating plainly:

- The managed Gmail server exposes create_draft and labeling but **not send**.
  So this connector's ``send_reply`` stays refused (inherits the safe default);
  the human sends the drafted message from Gmail. That's not a limitation we're
  working around — it's the safe pattern, and we keep it.

- This class talks to the MCP server through an injected ``mcp_call`` callable
  (``mcp_call(server, tool, arguments) -> dict``) rather than hard-wiring a
  transport. That keeps the connector testable and lets the actual MCP client
  (SDK, HTTP, whatever the deployment uses) be supplied from outside. The tool
  names are centralized below so a server-side rename is a one-line change.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from .base import (
    CalendarEvent,
    DraftRef,
    EmailThread,
    Provenance,
    WorkspaceConnector,
)

# Managed Google MCP server + tool identifiers, centralized.
GMAIL_SERVER = "gmail"
CALENDAR_SERVER = "calendar"

TOOL_SEARCH_THREADS = "search_threads"
TOOL_GET_THREAD = "get_thread"
TOOL_CREATE_DRAFT = "create_draft"
TOOL_LIST_EVENTS = "list_events"
TOOL_ADD_LABEL = "modify_labels"


McpCall = Callable[[str, str, dict[str, Any]], dict[str, Any]]


class McpWorkspaceConnector(WorkspaceConnector):
    """Workspace access via Google's managed MCP servers."""

    def __init__(self, mcp_call: McpCall):
        """``mcp_call(server, tool, arguments)`` performs one MCP tool call and
        returns the parsed result dict. Injected so transport/auth live outside
        this class and tests can supply a fake."""
        self._call = mcp_call

    def list_threads(
        self, query: str = "is:unread", *, max_results: int = 20
    ) -> list[EmailThread]:
        res = self._call(
            GMAIL_SERVER,
            TOOL_SEARCH_THREADS,
            {"query": query, "max_results": max_results},
        )
        return [self._to_thread(t) for t in res.get("threads", [])]

    def get_thread(self, thread_id: str) -> EmailThread:
        res = self._call(GMAIL_SERVER, TOOL_GET_THREAD, {"thread_id": thread_id})
        return self._to_thread(res)

    def list_events(
        self, *, time_min: datetime, time_max: datetime
    ) -> list[CalendarEvent]:
        res = self._call(
            CALENDAR_SERVER,
            TOOL_LIST_EVENTS,
            {"time_min": time_min.isoformat(), "time_max": time_max.isoformat()},
        )
        return [self._to_event(e) for e in res.get("events", [])]

    def create_draft(
        self, *, to: str, subject: str, body: str, thread_id: str | None = None
    ) -> DraftRef:
        args: dict[str, Any] = {"to": to, "subject": subject, "body": body}
        if thread_id:
            args["thread_id"] = thread_id
        res = self._call(GMAIL_SERVER, TOOL_CREATE_DRAFT, args)
        return DraftRef(draft_id=res.get("draft_id", ""), thread_id=thread_id)

    def add_label(self, *, thread_id: str, label: str) -> None:
        self._call(
            GMAIL_SERVER,
            TOOL_ADD_LABEL,
            {"thread_id": thread_id, "add_labels": [label]},
        )

    # send_reply intentionally NOT overridden: the managed Gmail server has no
    # send tool, so the safe draft-only default from the base class stands.

    @staticmethod
    def _to_thread(d: dict[str, Any]) -> EmailThread:
        # Bodies from the server are external content -> FETCHED/untrusted.
        return EmailThread(
            thread_id=d.get("thread_id") or d.get("id", ""),
            subject=d.get("subject", ""),
            snippet=d.get("snippet", ""),
            from_addr=d.get("from", ""),
            body=d.get("body", d.get("snippet", "")),
            provenance=Provenance.FETCHED,
            labels=d.get("labels", []),
        )

    @staticmethod
    def _to_event(d: dict[str, Any]) -> CalendarEvent:
        attendees = d.get("attendees", [])
        return CalendarEvent(
            event_id=d.get("event_id") or d.get("id", ""),
            summary=d.get("summary", ""),
            start=datetime.fromisoformat(d["start"]) if d.get("start") else datetime.min,
            end=datetime.fromisoformat(d["end"]) if d.get("end") else datetime.min,
            attendees=attendees,
            external_attendees=any("@" in a and not a.endswith("@telus.com")
                                   for a in attendees),
        )
