"""The Workspace connector interface (design doc 4.3, 4.7).

A small internal interface with two implementations behind it: a configured MCP
server and direct Google OAuth. Which one runs is a WorkspaceBackend choice, so
moving the credential/policy boundary is configuration rather than a rewrite.

Two safety facts are baked into this interface:

1. **Every fetched item carries provenance.** ``list_threads`` /
   ``get_thread`` return objects whose bodies are marked untrusted at the source,
   so the provenance discipline the orchestrator relies on is established here,
   at the boundary where external data enters — not left to a later layer to
   remember.

2. **Send is deliberately not a first-class verb.** The MCP contract exposes
   create_draft and labeling but not send — the safe pattern is
   "assistant drafts, human sends from Gmail." We mirror that: the primary write
   is ``create_draft``. ``send_reply`` exists on the interface but an
   implementation may raise ``SendNotPermitted``; the direct-OAuth implementation
   gates it behind an explicit scope + autonomy grant. This keeps the safe
   default (draft, don't send) structural rather than a matter of discipline.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


def has_external_attendees(
    attendees: list[str], internal_domains: frozenset[str]
) -> bool:
    """Return whether any attendee is outside the configured organization.

    With no internal domains configured, every address is treated as external;
    the autonomy gate must fail conservatively rather than assume trust.
    """
    normalized = {domain.lower().lstrip("@") for domain in internal_domains}
    for address in attendees:
        if "@" not in address:
            continue
        if address.rsplit("@", 1)[1].lower() not in normalized:
            return True
    return False


class Provenance(str, Enum):
    """Trust label attached to every piece of content entering the system.

    The whole indirect-prompt-injection defense rests on never letting
    UNTRUSTED content be treated as instructions. Marking it at the source makes
    that enforceable downstream."""

    USER_AUTHORED = "user_authored"   # the principal wrote it
    FETCHED = "fetched"               # came from email/chat/web -> UNTRUSTED


@dataclass
class EmailThread:
    """A minimal, provenance-tagged view of a mail thread. Bodies are FETCHED
    (untrusted) by construction.

    ``from_addr``/``received_at`` describe the thread's FIRST message (who
    started it, when); ``last_from_addr``/``last_message_at`` describe its
    LATEST message — the pair quiet-thread detection needs ("the user sent
    the last message N days ago, still no reply"; see ``brief.
    find_quiet_threads``)."""

    thread_id: str
    subject: str
    snippet: str
    from_addr: str
    body: str
    provenance: Provenance = Provenance.FETCHED
    received_at: datetime | None = None
    labels: list[str] = field(default_factory=list)
    last_from_addr: str = ""
    last_message_at: datetime | None = None
    # The correct reply target (review finding #3): the newest message NOT
    # authored by the owner, preferring its Reply-To header over From. Empty
    # when the thread has no counterparty (owner-only sent thread) — in
    # which case there is nobody to draft to.
    reply_to: str = ""


@dataclass
class CalendarEvent:
    event_id: str
    summary: str
    start: datetime
    end: datetime
    attendees: list[str] = field(default_factory=list)
    external_attendees: bool = False


@dataclass
class DraftRef:
    """Pointer to a created draft the human will review/send from Gmail."""

    draft_id: str
    thread_id: str | None = None


class ConnectorError(Exception):
    """Base for connector failures."""


class SendNotPermitted(ConnectorError):
    """Raised when send is attempted but not enabled for this connector.

    The safe default. Sending requires both an explicit OAuth send scope AND an
    autonomy grant; absent either, drafting is the only write path."""


class WorkspaceConnector(ABC):
    """The swappable Workspace boundary. Implementations: MCP, direct OAuth."""

    # --- read (both implementations support these) ---

    @abstractmethod
    def list_threads(
        self, query: str = "is:unread", *, max_results: int = 20
    ) -> list[EmailThread]:
        """Search mail. Returned bodies are FETCHED/untrusted."""

    @abstractmethod
    def get_thread(self, thread_id: str) -> EmailThread:
        """Fetch one thread. Body is FETCHED/untrusted."""

    @abstractmethod
    def list_events(
        self, *, time_min: datetime, time_max: datetime
    ) -> list[CalendarEvent]:
        """List calendar events in a window."""

    @abstractmethod
    def get_event(self, event_id: str) -> CalendarEvent:
        """Fetch one calendar event by id (the single-item counterpart to
        ``list_events``, mirroring ``get_thread``'s pairing with
        ``list_threads``). Used to turn a changed-event-id from Calendar
        ingestion into the details needed for scheduling logic (attendees,
        time), without ingestion itself depending on this interface."""

    # --- write (safe default: draft, don't send) ---

    @abstractmethod
    def create_draft(
        self, *, to: str, subject: str, body: str, thread_id: str | None = None
    ) -> DraftRef:
        """Create a draft for human review. The primary, safe write path."""

    def send_reply(self, *, draft_id: str) -> None:
        """Send a previously-created draft.

        Default implementation refuses: sending is opt-in per connector and
        gated by scope + autonomy elsewhere. Override only where send is
        genuinely enabled."""
        raise SendNotPermitted(
            "This connector is draft-only. Sending requires an explicit send "
            "scope and an autonomy grant; by default the human sends from Gmail."
        )

    def add_label(self, *, thread_id: str, label: str) -> None:
        """Apply a label (organizational, low-risk). Optional to implement."""
        raise NotImplementedError

    def create_hold(self, event: CalendarEvent) -> str:
        """Create a tentative calendar hold. Optional to implement."""
        raise NotImplementedError
