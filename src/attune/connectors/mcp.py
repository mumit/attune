"""MCP implementation of :class:`WorkspaceConnector`.

Targets any configured MCP package/server that implements Attune's small Gmail
and Calendar tool contract. Authentication and underlying provider credentials
belong to that server; Attune optionally authenticates to it with a bearer token.

Two things worth stating plainly:

- The contract exposes create_draft and labeling but **not send**.
  So this connector's ``send_reply`` stays refused (inherits the safe default);
  the human sends the drafted message from Gmail. That's not a limitation we're
  working around — it's the safe pattern, and we keep it.

- ``modify_labels``/``add_label`` above is add-only. The gated
  ``label_thread`` write path (Phase 3 stage 1, G9 — archiving triaged-NOISE
  mail, which needs to REMOVE the INBOX label) is deliberately NOT
  implemented here: contract v1 has no label-removal tool, so
  ``supports_labeling()`` stays at the base class's ``False`` and
  ``label_thread`` stays refused. New write actions land google_oauth-only
  until a v2 contract adds the capability (see ``docs/decisions.md``).

- Same posture again for ``decline_invite``/``reschedule_event`` (Phase 3
  stage 2): contract v1 has neither tool, so ``supports_calendar_writes()``
  stays the base class's ``False`` and both stay refused here regardless of
  whether the optional ``organizer``/``organizer_is_self``/
  ``response_status`` event fields below are populated.

- This class talks to the MCP server through an injected ``mcp_call`` callable
  (``mcp_call(server, tool, arguments) -> dict``) rather than hard-wiring a
  transport. That keeps the connector testable while the live runtime supplies
  the official SDK's Streamable HTTP client. The tool
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
    has_external_attendees,
)

# Logical service and tool identifiers, centralized as the public contract.
MCP_CONTRACT_VERSION = "1"
GMAIL_SERVER = "gmail"
CALENDAR_SERVER = "calendar"

TOOL_SEARCH_THREADS = "search_threads"
TOOL_GET_THREAD = "get_thread"
TOOL_CREATE_DRAFT = "create_draft"
TOOL_LIST_EVENTS = "list_events"
TOOL_GET_EVENT = "get_event"
TOOL_ADD_LABEL = "modify_labels"

MCP_REQUIRED_TOOLS = {
    GMAIL_SERVER: frozenset(
        {TOOL_SEARCH_THREADS, TOOL_GET_THREAD, TOOL_CREATE_DRAFT, TOOL_ADD_LABEL}
    ),
    CALENDAR_SERVER: frozenset({TOOL_LIST_EVENTS, TOOL_GET_EVENT}),
}


McpCall = Callable[[str, str, dict[str, Any]], dict[str, Any]]


class McpWorkspaceConnector(WorkspaceConnector):
    """Workspace access via a configured MCP server or package."""

    def __init__(
        self, mcp_call: McpCall, *, internal_domains: frozenset[str] = frozenset()
    ):
        """``mcp_call(server, tool, arguments)`` performs one MCP tool call and
        returns the parsed result dict. Injected so transport/auth live outside
        this class and tests can supply a fake."""
        self._call = mcp_call
        self._internal_domains = internal_domains

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

    def get_event(self, event_id: str) -> CalendarEvent:
        res = self._call(CALENDAR_SERVER, TOOL_GET_EVENT, {"event_id": event_id})
        return self._to_event(res)

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
        last_at = d.get("last_message_at")
        if isinstance(last_at, str):
            try:
                from datetime import datetime as _dt

                last_at = _dt.fromisoformat(last_at)
            except ValueError:
                last_at = None
        return EmailThread(
            thread_id=d.get("thread_id") or d.get("id", ""),
            subject=d.get("subject", ""),
            snippet=d.get("snippet", ""),
            from_addr=d.get("from", ""),
            body=d.get("body", d.get("snippet", "")),
            provenance=Provenance.FETCHED,
            labels=d.get("labels", []),
            last_from_addr=d.get("last_from", d.get("from", "")),
            last_message_at=last_at,
            reply_to=d.get("reply_to", ""),
        )

    def _to_event(self, d: dict[str, Any]) -> CalendarEvent:
        attendees = d.get("attendees", [])
        return CalendarEvent(
            event_id=d.get("event_id") or d.get("id", ""),
            summary=d.get("summary", ""),
            start=datetime.fromisoformat(d["start"]) if d.get("start") else datetime.min,
            end=datetime.fromisoformat(d["end"]) if d.get("end") else datetime.min,
            attendees=attendees,
            external_attendees=has_external_attendees(
                attendees, self._internal_domains
            ),
            # Phase 3 stage 2: OPTIONAL pass-through fields, backward
            # compatible with a server that doesn't emit them (defaults to
            # "read nothing decided" — never a false positive for
            # decline/reschedule detection). Contract v1 has no
            # decline/reschedule tool regardless of what these report —
            # supports_calendar_writes() stays the base class's False.
            organizer=d.get("organizer", ""),
            organizer_is_self=bool(d.get("organizer_is_self", False)),
            response_status=d.get("response_status", ""),
        )
