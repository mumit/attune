"""Deterministic cross-source correlation (``docs/future-state.md`` Phase 2,
step 2; ``docs/gap-analysis.md`` G3).

Today an urgent email and a same-topic Slack thread are two unrelated items —
nothing links them, so the brief (and any future notification surface) shows
one topic as two separate entries. This module is the deterministic,
model-free seam that groups items about the same topic/thread across mail,
attended Slack/Chat sources, and calendar events, so a caller (``brief.py``)
can present one correlated group instead of several duplicates.

Per the Phase 2 plan, embedding similarity is explicitly deferred — this
stage is "participants + subject/entity matching first". Everything here is
a pure function: no I/O, no model calls, no randomness.

**Product behavior — the linking rule (conservative on purpose):**

Two items link when EITHER of the following holds. A false merge (unrelated
items shown as one) is worse than a false miss (related items shown
separately, same as today) — every threshold below is chosen to keep merges
rare rather than to maximize recall:

1. **Participant overlap.** The two items share at least one exact,
   normalized participant token, where a token is either a lowercased email
   address, or a lowercased display name of **two or more words**. A shared
   single word (a bare first name, or a Slack/Chat sender ref that happens to
   collide) never links two items on its own — "John" appearing in two
   unrelated senders' names must not merge them. See :func:`_participant_tokens`.
2. **Topic overlap.** Significant-token overlap between each item's
   ``text`` (title/summary, lowercased, punctuation stripped, a small
   built-in stopword list and tokens shorter than 4 characters dropped).
   Linking requires **at least 2 shared significant tokens**, OR — only when
   the smaller side has at least 2 significant tokens to begin with — the
   shared count is at least half the smaller side's token count. That
   "smaller side has 2+ tokens" guard exists specifically so a single shared
   token can never link two items by itself, however small the other side's
   token set is; see :func:`_topic_overlap`.

Grouping itself is a standard union-find over all pairs: any chain of
pairwise links merges transitively into one group (A-B and B-C merges A, B,
and C together even if A and C never link directly). Groups are returned
sorted by their earliest item's timestamp, and items within a group are
sorted by timestamp too, so output order is stable given the same input.

Hosted seam (``docs/future-state.md`` Phase 5 item 1, gap G18): this whole
module is already a pure function of caller-supplied data — no store, no
clock, no I/O — so it needs no hosted counterpart at all. A future hosted
brief assembler calls :func:`correlate`/:func:`from_attention_item` directly,
the same as ``brief.py`` does locally, over
``attune.hosted.intelligence.PostgresAttentionStore.recent()`` results
instead of :class:`~orchestrator.attention.JsonAttentionStore` ones.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from ..connectors.base import CalendarEvent, EmailThread
from .attention import AttentionItem

# Topic-overlap thresholds (product behavior, documented above) — kept as
# module constants so tests and any future tuning have one place to look.
MIN_SHARED_TOPIC_TOKENS = 2
MIN_TOPIC_OVERLAP_RATIO = 0.5
MIN_TOKEN_LEN = 4

# A small, deliberately conservative stopword list: common English function
# words and generic business-message filler that would otherwise inflate
# apparent topic overlap between genuinely unrelated messages.
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "this", "that", "have", "has",
    "are", "was", "were", "will", "your", "you", "about", "please",
    "thanks", "thank", "regarding", "following", "there", "their", "would",
    "could", "should", "which", "while", "before", "after", "again",
    "being", "between", "where", "does", "done", "into", "just", "also",
    "here", "when", "what", "than", "them", "they", "then",
})

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# Epoch fallback for a mail thread with no timestamp at all (rare, but
# ``EmailThread.received_at``/``last_message_at`` are both optional) — never
# raises, never needs a live clock.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class CorrelatableItem:
    """One item normalized for correlation, whatever source it came from.

    ``origin`` is the original object (:class:`~connectors.base.EmailThread`,
    :class:`~orchestrator.attention.AttentionItem`, or
    :class:`~connectors.base.CalendarEvent`) — carried through unchanged so a
    caller can read source-specific fields (sender, priority,
    mentions_principal, ...) straight off it rather than this module
    inventing duplicate fields for every possible consumer.
    """

    kind: str  # "mail" | "source" | "calendar"
    ref: str
    text: str
    participants: frozenset[str]
    ts: datetime
    origin: Any


def _participant_tokens(raw: str) -> set[str]:
    """Normalize one free-form participant string into zero or more tokens.

    Handles ``"Display Name <email@x.com>"``, a bare address, or a bare
    display name (Slack/Chat's ``sender_display``). Returns the lowercased
    email if one is found, plus the lowercased display-name portion IF it is
    two or more words — a single-word name is dropped rather than kept as a
    token, which is what makes the "no linking on a common first name" rule
    hold structurally rather than by convention.
    """
    raw = (raw or "").strip()
    if not raw:
        return set()
    tokens: set[str] = set()
    match = _EMAIL_RE.search(raw)
    if match:
        tokens.add(match.group(0).lower())
        name_part = raw[: match.start()].strip(" <\"'")
    else:
        name_part = raw
    if name_part and len(name_part.split()) >= 2:
        tokens.add(name_part.lower())
    return tokens


def _significant_tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) >= MIN_TOKEN_LEN and w not in _STOPWORDS}


# ---------------------------------------------------------------------------
# Builders — from the real shapes brief.py already has.
# ---------------------------------------------------------------------------


def from_mail_thread(thread: EmailThread, *, now: datetime | None = None) -> CorrelatableItem:
    """Build a :class:`CorrelatableItem` from an already-fetched mail thread.

    ``now`` is only used as a last-resort timestamp fallback when a thread
    somehow has neither ``last_message_at`` nor ``received_at`` — callers
    pass the same ``now`` ``assemble_brief`` already computes. Reads
    ``last_from_addr``/``last_message_at``/``received_at`` via ``getattr``
    with a safe default (mirrors ``dispatcher._mail_source``'s same
    defensive read) since minimal test doubles for ``EmailThread`` in this
    codebase sometimes omit the optional fields."""
    participants = _participant_tokens(thread.from_addr) | _participant_tokens(
        getattr(thread, "last_from_addr", "") or ""
    )
    ts = (
        getattr(thread, "last_message_at", None)
        or getattr(thread, "received_at", None)
        or now
        or _EPOCH
    )
    return CorrelatableItem(
        kind="mail",
        ref=thread.thread_id,
        text=f"{thread.subject} {thread.snippet}",
        participants=frozenset(participants),
        ts=ts,
        origin=thread,
    )


def from_attention_item(item: AttentionItem) -> CorrelatableItem:
    """Build a :class:`CorrelatableItem` from a recorded Slack/Chat source
    signal (``orchestrator/attention.py``)."""
    participants = _participant_tokens(item.sender_ref) | _participant_tokens(
        item.sender_display
    )
    ref = f"{item.source}:{item.channel_ref}:{item.thread_ref or item.ts.isoformat()}"
    return CorrelatableItem(
        kind="source",
        ref=ref,
        text=f"{item.channel_name} {item.summary}",
        participants=frozenset(participants),
        ts=item.ts,
        origin=item,
    )


def from_calendar_event(event: CalendarEvent) -> CorrelatableItem:
    """Build a :class:`CorrelatableItem` from a today's-calendar event."""
    participants: set[str] = set()
    for attendee in event.attendees:
        participants |= _participant_tokens(attendee)
    return CorrelatableItem(
        kind="calendar",
        ref=event.event_id,
        text=event.summary,
        participants=frozenset(participants),
        ts=event.start,
        origin=event,
    )


# ---------------------------------------------------------------------------
# Linking + grouping
# ---------------------------------------------------------------------------


def _participants_link(a: frozenset[str], b: frozenset[str]) -> bool:
    """Rule 1 (participant overlap): any exactly-shared token is sufficient
    on its own — ``_participant_tokens`` already dropped single-word names,
    so a shared token here is always either an exact email match or an
    exact 2+-word display-name match."""
    return bool(a & b)


def _topic_overlap(a: CorrelatableItem, b: CorrelatableItem) -> bool:
    """Rule 2 (topic overlap): see the module docstring for the exact bar.
    The ``smaller >= 2`` guard on the ratio branch means a single shared
    significant token is never enough to link two items, regardless of how
    small the other item's token set is."""
    tokens_a = _significant_tokens(a.text)
    tokens_b = _significant_tokens(b.text)
    if not tokens_a or not tokens_b:
        return False
    shared = tokens_a & tokens_b
    if len(shared) >= MIN_SHARED_TOPIC_TOKENS:
        return True
    smaller = min(len(tokens_a), len(tokens_b))
    if smaller >= 2 and (len(shared) / smaller) >= MIN_TOPIC_OVERLAP_RATIO:
        return True
    return False


def _should_link(a: CorrelatableItem, b: CorrelatableItem) -> bool:
    return _participants_link(a.participants, b.participants) or _topic_overlap(a, b)


def correlate(items: Sequence[CorrelatableItem]) -> list[list[CorrelatableItem]]:
    """Group items that link (directly or transitively) via
    :func:`_should_link`, using a standard union-find over all pairs.

    Returns groups sorted by their earliest item's ``ts``; items within each
    group are sorted by ``ts`` too. A singleton (unlinked) item is still
    returned as its own one-item group — nothing is ever dropped here."""
    n = len(items)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for i in range(n):
        for j in range(i + 1, n):
            if _should_link(items[i], items[j]):
                union(i, j)

    grouped: dict[int, list[CorrelatableItem]] = {}
    for i in range(n):
        grouped.setdefault(find(i), []).append(items[i])

    groups = list(grouped.values())
    for group in groups:
        group.sort(key=lambda it: it.ts)
    groups.sort(key=lambda group: group[0].ts)
    return groups
