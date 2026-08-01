"""Batch approval cards (build prompt 31, tasks 4 & 5).

One card, one ``lg_tid``, three buttons, no aggregate handler — on a busy
morning that turns individually-trivial decisions into a stream, and the
named failure mode of approval gates is precisely that *"the control breaks
when approval stops being a real decision and becomes a reflex."* This
module is the batching counter-measure: when several proposals of the SAME
capability (``(domain, action)``) are pending at once, they render as one
grouped card instead of N separate ones.

Vocabulary: **accept / edit / respond / ignore** — the category's shared
language (LangChain's Agent Inbox), adopted here rather than reinventing
"approve/edit/reject/skip."

The hard constraint this module exists to satisfy (task 4/5's own words):

- **"Approve all" is never a new aggregate effect.** It expands into N
  individually-audited resumes through the EXACT SAME
  ``draft_approve.resume_workflow`` every single-item Approve button
  already calls — see :func:`resolve_batch_approve_all`. Each item keeps
  its own ``lg_tid``, its own freshness check (inside ``apply``), its own
  ``pending.claim``, and its own decision-ledger row. There is no code
  path in this module that writes one audit row for the whole batch.
- **A partially-processed batch is safe to retry.** A double-click on
  "approve all" (or a race with someone answering one item individually
  first) applies nothing further for an already-claimed item —
  ``pending.claim`` returns ``False`` for it, and ``resume_workflow``
  short-circuits before ever invoking the graph. This is the SAME
  claim-then-skip machinery every other resume path already relies on;
  batching adds no new race.
- **Never a way to approve something the principal didn't see.** Every
  item in a grouped card is individually rendered with its subject/
  recipient (:func:`render_batch_card`) — an approve-all over a truncated
  list is forbidden, so this module never elides an entry to keep a card
  short.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .pending import STATUS_PENDING, PendingApproval

# Groups smaller than this render as today's ordinary single card, not a
# "batch of one" — batching only matters once there's actually a stream of
# same-capability decisions to compress.
MIN_BATCH_SIZE = 2


@dataclass(frozen=True)
class BatchGroup:
    """Several pending proposals sharing one capability, ready to render as
    one grouped card."""

    domain: str
    action: str
    entries: "tuple[PendingApproval, ...]"

    @property
    def thread_ids(self) -> "tuple[str, ...]":
        return tuple(e.lg_tid for e in self.entries)

    def __len__(self) -> int:
        return len(self.entries)


def group_pending_by_capability(
    entries: "Sequence[PendingApproval]", *, min_size: int = MIN_BATCH_SIZE,
) -> "list[BatchGroup]":
    """Group currently-pending entries by ``(domain, action)`` — "several
    proposals of the same capability" (task 4). An entry with no recorded
    ``action`` (a card posted before build prompt 31, or a caller that
    never set one) never joins a group — an unknown capability cannot be
    safely batched, so it simply keeps rendering as its own ordinary card.
    Groups smaller than ``min_size`` are dropped for the same reason.
    """
    by_key: "dict[tuple[str, str], list[PendingApproval]]" = {}
    for entry in entries:
        if entry.status != STATUS_PENDING or not entry.action:
            continue
        by_key.setdefault((entry.domain, entry.action), []).append(entry)
    groups = [
        BatchGroup(domain=domain, action=action, entries=tuple(items))
        for (domain, action), items in by_key.items()
        if len(items) >= min_size
    ]
    groups.sort(key=lambda g: (g.domain, g.action))
    return groups


def render_batch_card(group: "BatchGroup") -> str:
    """Channel-agnostic text rendering of one grouped card — every item
    individually named with its subject and counterparty (never truncated;
    see the module docstring's "never a way to approve something the
    principal didn't see" constraint), using the accept/edit/respond/ignore
    vocabulary.
    """
    lines = [
        f"{len(group.entries)} pending {group.action} proposals on "
        f"{group.domain}:",
    ]
    for i, entry in enumerate(group.entries, start=1):
        counterpart = f" — {entry.sender}" if entry.sender else ""
        subject = entry.subject or entry.source_ref
        lines.append(f"  {i}. {subject}{counterpart}")
    lines.append("")
    lines.append(
        "Reply to one item (accept / edit / respond / ignore), or approve all."
    )
    return "\n".join(lines)


def resolve_batch_approve_all(
    thread_ids: "Sequence[str]",
    *,
    graph: Any = None,
    resume: "Callable[[str], Any] | None" = None,
    pending: Any = None,
    audit_log: Any = None,
    user_id: "str | None" = None,
    ledger: Any = None,
    store: Any = None,
    actor: "str | None" = None,
) -> "list[Any]":
    """Expand "approve all" into N individually-audited resumes.

    ``resume`` (a one-argument callable over a thread_id) is the injection
    seam for tests; production omits it and gets a per-item
    ``draft_approve.resume_workflow(graph, thread_id, "approved", ...)``
    call — the SAME function every single-item Approve button already
    calls, so nothing about freshness checking, claiming, auditing, or
    ledger-completion is reimplemented here. Returns one result dict per
    thread_id, in the order given — a repeated/overlapping call over the
    same ids is safe (see the module docstring): an already-claimed item's
    result carries ``approval_already_handled``, not a second effect.
    """
    def _default_resume(thread_id: str) -> Any:
        from .draft_approve import resume_workflow

        return resume_workflow(
            graph, thread_id, "approved",
            pending=pending, audit_log=audit_log, user_id=user_id,
            actor=actor, ledger=ledger, store=store,
        )

    resume_fn = resume if resume is not None else _default_resume
    return [resume_fn(thread_id) for thread_id in thread_ids]
