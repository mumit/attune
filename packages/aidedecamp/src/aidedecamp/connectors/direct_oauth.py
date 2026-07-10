"""Direct-OAuth implementation of :class:`WorkspaceConnector` (design 4.3, 4.7).

The fallback for when managed MCP servers aren't permitted (e.g. a TELUS
governance decision): talk to the Google APIs directly via
google-api-python-client with a per-deployment OAuth credential.

This is a documented stub for now — the interface, the send-gating design, and
the scope discipline are pinned down here so filling it in is mechanical. The
key design point, and the reason send-gating lives in *this* implementation
rather than the MCP one: direct OAuth is the only path that *can* technically
send, so it's the only place a send scope + autonomy grant could unlock it. The
MCP path can't send at all (no tool), which is why it's the safer default.

Scope discipline (design + Google guidance): start read-only
(``gmail.readonly``, ``calendar.readonly``), add ``gmail.compose`` for drafting,
and add ``gmail.send`` ONLY when an autonomy grant explicitly calls for
autonomous sending. Never request send scope "to avoid re-auth later."
"""

from __future__ import annotations

from datetime import datetime

from .base import (
    CalendarEvent,
    DraftRef,
    EmailThread,
    SendNotPermitted,
    WorkspaceConnector,
)

# Minimal, escalating scope sets. Compose the set from the capabilities actually
# granted; do not request send unless autonomous sending is authorized.
SCOPES_READONLY = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
)
SCOPE_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
SCOPE_SEND = "https://www.googleapis.com/auth/gmail.send"


class DirectOAuthConnector(WorkspaceConnector):
    """Direct Google API access. Send is gated behind an explicit flag that a
    caller sets ONLY alongside the gmail.send scope and an autonomy grant."""

    def __init__(self, *, credentials=None, send_enabled: bool = False):
        self._creds = credentials
        self._send_enabled = send_enabled  # must mirror a real gmail.send grant

    def list_threads(self, query="is:unread", *, max_results=20) -> list[EmailThread]:
        raise NotImplementedError("DirectOAuthConnector: implement in Phase 1")

    def get_thread(self, thread_id: str) -> EmailThread:
        raise NotImplementedError("DirectOAuthConnector: implement in Phase 1")

    def list_events(self, *, time_min: datetime, time_max: datetime) -> list[CalendarEvent]:
        raise NotImplementedError("DirectOAuthConnector: implement in Phase 1")

    def create_draft(self, *, to, subject, body, thread_id=None) -> DraftRef:
        raise NotImplementedError("DirectOAuthConnector: implement in Phase 1")

    def send_reply(self, *, draft_id: str) -> None:
        # Even once implemented, refuse unless send was explicitly enabled.
        if not self._send_enabled:
            raise SendNotPermitted(
                "DirectOAuthConnector send disabled: requires gmail.send scope "
                "AND an autonomy grant. Draft-and-human-send is the default."
            )
        raise NotImplementedError("DirectOAuthConnector send: implement in Phase 1")
