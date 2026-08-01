"""Meeting-context ingestion over MCP (build prompt 34, task 5).

Granola, Circleback, and comparable services now expose an MCP server over
already-consented meeting capture. Attune does **not** build recording — see
`docs/decisions.md` for the declined-browser/recording decision and its
citation of the 2026 non-consensual-recording standing ruling. This module
instead *consumes* someone else's consented capture as one more read-only
signal source, through the same :class:`~orchestrator.attention.AttentionStore`
every other source (`ingestion/sources.py`'s Slack/Chat pattern) already
feeds, so meeting content becomes an importance signal without Attune ever
holding a microphone. Consuming is a materially different legal and product
posture than capturing, which is the entire point of doing it this way.

**No write path exists here, structurally**: :class:`MeetingNote` carries no
field or method that round-trips back to the meeting tool, and
:func:`poll_meeting_source`'s only effect is one
:meth:`~orchestrator.attention.AttentionStore.add` call per note — the same
"triage and record, never reply/write" posture
``dispatcher.handle_source_message`` already holds for Slack/Chat sources,
so a successful prompt injection inside a meeting note's summary text has no
write surface to reach.

Cursor discipline mirrors `ingestion/sources.py` exactly: a generic
per-provider high-water mark (the same key-value state store Slack/Chat
polling already uses), first run baselines to "now" and dispatches nothing,
and a downstream failure enqueues a durable retry rather than blocking or
silently dropping the note.

Transport is the same injected ``mcp_call`` shape
`connectors/mcp.py`'s :class:`McpWorkspaceConnector` already uses
(``mcp_call(server, tool, arguments) -> dict``) — the logical server name
here is :data:`MCP_SERVER`, distinct from the Gmail/Calendar contract's
``"gmail"``/``"calendar"`` servers, so a deployment can point it at a
completely different MCP server/package (Granola, Circleback, ...) without
touching the workspace connector at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from ..orchestrator.attention import AttentionItem
from ..orchestrator.triage import Priority

# Meeting summaries can be long transcripts-worth of text; bounded exactly
# like ingestion/sources.py's SourceMessage.text, for the same reason (never
# let an unbounded fetched body blow up a downstream prompt or store).
TEXT_CHAR_CAP = 2000
MEETING_POLL_MAX_NOTES = 50

MCP_SERVER = "meetings"
TOOL_LIST_MEETING_NOTES = "list_meeting_notes"


@dataclass(frozen=True)
class MeetingNote:
    """One normalized, provenance-tagged meeting note ready for triage.
    ``provider`` is the configured logical source (e.g. ``"granola"``) —
    kept distinct from ``meeting_id`` so two providers can never collide in
    the attention store or the poll cursor."""

    meeting_id: str
    title: str
    attendees: "tuple[str, ...]"
    summary: str
    occurred_at: datetime
    provider: str

    def __post_init__(self) -> None:
        if len(self.summary) > TEXT_CHAR_CAP:
            object.__setattr__(self, "summary", self.summary[:TEXT_CHAR_CAP])


def _parse_occurred_at(raw: "dict[str, Any]") -> datetime:
    value = raw.get("occurred_at") or raw.get("start")
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def meeting_note_from_mcp(raw: "dict[str, Any]", *, provider: str) -> MeetingNote:
    """Build a :class:`MeetingNote` from one MCP ``list_meeting_notes``
    result item. Every field defaults to an empty/safe value — a
    conformant server need only ever return more than the minimum, never
    exactly this shape, matching the Gmail/Calendar contract's own "missing
    descriptive fields default to empty" posture (`docs/mcp-contract.md`)."""
    return MeetingNote(
        meeting_id=raw.get("meeting_id") or raw.get("id") or "",
        title=raw.get("title", ""),
        attendees=tuple(raw.get("attendees", [])),
        summary=raw.get("summary") or raw.get("notes") or "",
        occurred_at=_parse_occurred_at(raw),
        provider=provider,
    )


def meeting_note_to_attention_item(note: MeetingNote) -> AttentionItem:
    """Meeting content is untrusted, fetched signal — exactly like a
    Slack/Chat source message — triaged into the same bounded
    :class:`AttentionItem` shape every other source produces, never a
    distinct, higher-trust record. Priority is always ROUTINE: nothing
    about meeting-note content is a trusted urgency signal (the same
    reasoning `orchestrator/triage.py`'s forged-signal defenses already
    apply to message bodies applies here to note text)."""
    attendee_display = ", ".join(note.attendees[:5]) or "unknown attendees"
    return AttentionItem(
        source="meeting",
        channel_ref=f"{note.provider}:{note.meeting_id}",
        channel_name=note.title or note.meeting_id,
        sender_ref=note.provider,
        sender_display=attendee_display,
        summary=note.summary or note.title,
        ts=note.occurred_at,
        priority=Priority.ROUTINE,
        # The principal is, by construction, a participant in their own
        # captured meeting -- unlike a Slack/Chat source message, there is
        # no separate "was I mentioned" question to ask.
        mentions_principal=True,
        thread_ref=None,
    )


def poll_meeting_source(
    mcp_call: "Callable[[str, str, dict[str, Any]], dict[str, Any]]",
    state: Any,
    attention_store: Any,
    *,
    provider: str,
    retry_queue: Any = None,
    max_notes: int = MEETING_POLL_MAX_NOTES,
    now: "Callable[[], datetime] | None" = None,
) -> int:
    """One meeting-source poll tick.

    ``state`` is the generic per-key high-water-mark store
    (:class:`~ingestion.state.JsonChatPollState`, reused exactly as
    Slack/Chat source polling already does), keyed ``f"meeting:{provider}"``.

    First run (no stored cursor): baseline to "now", record nothing, return
    0 — never replay meeting history from before Attune was configured to
    read it.

    Otherwise: fetch up to ``max_notes`` notes since the stored cursor via
    ``mcp_call(MCP_SERVER, TOOL_LIST_MEETING_NOTES, {"since": ..., "max_results": ...})``,
    advance the cursor to the newest note's ``occurred_at`` immediately —
    before any attention-store write is attempted, the same "cursor
    advances on successful listing, independent of downstream per-item
    success" discipline every other poller in this package holds — then
    record each note. A record failure enqueues a durable retry (the
    ``"meeting_source"`` kind) rather than blocking or losing the note;
    without a ``retry_queue`` the exception propagates, matching every
    other poller's direct/test-caller fallback.

    Returns the number of notes considered (recorded or retried).
    """
    clock = now or (lambda: datetime.now(timezone.utc))
    key = f"meeting:{provider}"
    existing = state.get(key) or {}
    since = existing.get("last_seen")
    if since is None:
        state.put(key, last_seen=clock().astimezone(timezone.utc).isoformat())
        return 0

    response = mcp_call(
        MCP_SERVER, TOOL_LIST_MEETING_NOTES, {"since": since, "max_results": max_notes}
    )
    raw_notes = list(response.get("notes", []))[:max_notes]
    parsed = [meeting_note_from_mcp(raw, provider=provider) for raw in raw_notes]

    new_cursor = since
    for note in parsed:
        candidate = note.occurred_at.astimezone(timezone.utc).isoformat()
        if candidate > new_cursor:
            new_cursor = candidate
    if new_cursor != since:
        state.put(key, last_seen=new_cursor)

    considered = 0
    for raw, note in zip(raw_notes, parsed):
        considered += 1
        try:
            attention_store.add(meeting_note_to_attention_item(note), now=clock())
        except Exception as exc:  # noqa: BLE001 — durable retry, never silent
            if retry_queue is None:
                raise
            retry_queue.enqueue(
                "meeting_source",
                f"{provider}:{note.meeting_id}",
                {"provider": provider, "raw": raw},
                error=type(exc).__name__,
            )
    return considered
