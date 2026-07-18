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

3. **Labeling/archiving mirrors the same structural-refusal shape** (Phase 3
   stage 1, ``docs/future-state.md`` G9). ``label_thread`` is the real write
   path for the first hygiene action (archiving triaged-NOISE mail);
   ``supports_labeling()`` is the capability probe callers check BEFORE even
   attempting it (send has no equivalent probe — a caller just tries
   ``send_reply`` and catches ``SendNotPermitted`` — but the dispatcher needs
   to decide whether to build a proposal at all, which send's fire-and-catch
   shape doesn't support). The base class default is the same conservative
   posture as send: refuse (``LabelNotPermitted``) and report no support,
   until an implementation deliberately opts in.

4. **Calendar writes (decline/reschedule) mirror the same shape again**
   (Phase 3 stage 2). ``decline_invite``/``reschedule_event`` are the real
   write paths for the second and third hygiene actions; both live behind
   ``supports_calendar_writes()`` and both structural refusals
   (``CalendarWriteNotPermitted``). ``reschedule_event`` additionally
   requires implementations to re-verify the principal is the event's
   organizer from a FRESH fetch — never from anything a caller passes in or
   a workflow checkpoint remembers — since a stale belief about who
   organizes a meeting is exactly the kind of mistake a structural refusal
   exists to prevent.
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
    # Phase 3 stage 2 (decline-invite/reschedule proposals): the organizer's
    # email — empty when the source connector/backend doesn't populate it,
    # the safe back-compat default, under which neither DECLINE_INVITE's
    # LOW-tier reason nor RESCHEDULE's organizer check can ever fire for
    # that event. ``organizer_is_self`` is the separate, more reliable
    # "does the PRINCIPAL organize this event" signal used to decide
    # RESCHEDULE eligibility (Deliverable C) — read straight from the
    # provider's own organizer.self flag rather than an email-string
    # comparison against configuration, so it survives aliases and is
    # always fail-closed (False) absent a positive confirmation.
    organizer: str = ""
    organizer_is_self: bool = False
    # The PRINCIPAL's own attendee responseStatus ("needsAction",
    # "accepted", "declined", "tentative"), or "" when absent/not an
    # attendee/not populated by the backend — DECLINE_INVITE's detection
    # key (Deliverable B). Back-compat: a connector that doesn't populate
    # this defaults to "", which never matches "needsAction", so no invite
    # is ever mistakenly proposed for decline.
    response_status: str = ""


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


class LabelNotPermitted(ConnectorError):
    """Raised when label_thread is attempted but not enabled for this connector.

    The safe default, mirroring ``SendNotPermitted``. Labeling requires both
    an explicit OAuth scope (``gmail.modify``) AND the deployment's own
    ``ATTUNE_MAIL_LABELS_ENABLED`` opt-in; absent either, no thread is ever
    labeled or archived. The MCP adapter never overrides this — contract v1
    has no tool that can remove a label (needed for archiving), so it stays
    google_oauth-only pending a v2 contract (see docs/decisions.md)."""


class CalendarWriteNotPermitted(ConnectorError):
    """Raised when decline_invite/reschedule_event is attempted but not
    enabled for this connector, or when the principal isn't the right party
    for the requested write (Phase 3 stage 2).

    The same double-gate discipline as ``LabelNotPermitted``: a
    ``calendar_writes_enabled`` flag set ONLY alongside a real calendar
    write scope AND the deployment's own ``ATTUNE_CALENDAR_WRITES_ENABLED``
    opt-in. The MCP adapter never overrides this — contract v1 has neither
    tool, so it stays google_oauth-only pending a v2 contract (see
    docs/decisions.md). ``DirectOAuthConnector`` also raises this when a
    fresh event fetch shows the principal isn't an attendee (decline) or
    isn't the organizer (reschedule) — a structural refusal, not just a
    disabled-flag refusal."""


# The archive-proposal write path's default label (Phase 3 stage 1, G9). A
# single shared constant so the dispatcher (which builds the proposal state)
# and the connector (which creates the label if missing) always agree on the
# name without either importing the other's concrete implementation.
DEFAULT_NOISE_LABEL = "Attune/Noise"


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

    def supports_labeling(self) -> bool:
        """Capability probe for :meth:`label_thread` (Phase 3 stage 1, G9).

        False by default. A caller (the dispatcher) checks this BEFORE
        deciding whether to build an archive proposal at all — one of three
        independent gates (matrix rung, this probe, the deployment's opt-in
        flag) that must all hold for the write path to ever be reached."""
        return False

    def label_thread(self, thread_id: str, *, label: str, archive: bool) -> None:
        """Apply ``label`` to a thread, optionally archiving it (removing it
        from the inbox). The real write path for hygiene actions (Phase 3
        stage 1, G9): a human approves an archive proposal, and this is what
        materializes that decision.

        Default implementation refuses: labeling is opt-in per connector,
        gated by an OAuth scope and an explicit deployment flag, and the
        MCP adapter never overrides this (contract v1 has no label-removal
        tool — see ``docs/mcp-contract.md`` and ``docs/decisions.md``)."""
        raise LabelNotPermitted(
            "This connector cannot label/archive threads. Direct OAuth "
            "requires the gmail.modify scope AND ATTUNE_MAIL_LABELS_ENABLED; "
            "MCP has no such capability in contract v1."
        )

    def create_hold(self, event: CalendarEvent) -> str:
        """Create a tentative calendar hold. Optional to implement."""
        raise NotImplementedError

    def supports_calendar_writes(self) -> bool:
        """Capability probe for :meth:`decline_invite`/:meth:`reschedule_event`
        (Phase 3 stage 2), mirroring :meth:`supports_labeling`. False by
        default. A caller (the dispatcher) checks this BEFORE building a
        decline or reschedule proposal at all — one of three independent
        gates (matrix rung, this probe, the deployment's opt-in flag) that
        must all hold for either write path to ever be reached."""
        return False

    def decline_invite(self, event_id: str) -> None:
        """Decline the calendar invite at ``event_id`` on the PRINCIPAL's
        own behalf (Phase 3 stage 2) — patches only their own attendee
        responseStatus, never anyone else's.

        Default implementation refuses: calendar writes are opt-in per
        connector, gated by an OAuth scope and an explicit deployment flag,
        and the MCP adapter never overrides this (contract v1 has no
        decline tool — see ``docs/mcp-contract.md`` and
        ``docs/decisions.md``)."""
        raise CalendarWriteNotPermitted(
            "This connector cannot decline calendar invites. Direct OAuth "
            "requires a calendar write scope AND "
            "ATTUNE_CALENDAR_WRITES_ENABLED; MCP has no such capability in "
            "contract v1."
        )

    def reschedule_event(
        self, event_id: str, *, new_start: datetime, new_end: datetime
    ) -> None:
        """Move the event at ``event_id`` to a new start/end (Phase 3 stage
        2) — implementations MUST refuse when the principal is not this
        event's organizer, verified from a FRESH fetch, never from cached
        workflow state.

        Default implementation refuses the same way :meth:`decline_invite`
        does: calendar writes are opt-in per connector and MCP never
        overrides this."""
        raise CalendarWriteNotPermitted(
            "This connector cannot reschedule calendar events. Direct "
            "OAuth requires a calendar write scope AND "
            "ATTUNE_CALENDAR_WRITES_ENABLED; MCP has no such capability in "
            "contract v1."
        )
