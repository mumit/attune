"""Graph state for Attune workflows (design doc 4.2).

State schema is the most consequential decision in a LangGraph project, for one
specific reason: everything in state is serialized to the checkpoint on every
node transition, and accumulator fields survive restarts while overwrite fields
take their last written value. Getting the accumulator/overwrite split wrong
causes two classic bugs — silently doubled lists on resume, and state bloat that
slows checkpoint writes. So the split is made explicit and deliberate here.

Accumulator fields (Annotated[..., add]) — grow across the workflow's life:
    audit_events   every reason-for-action entry (design 4.7)
Overwrite fields (plain types) — current value only:
    everything else: the item being handled, the current draft, the decision.

We deliberately keep large blobs (raw email bodies, full model responses) OUT of
state. State holds pointers and the current draft, not transcripts.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, Optional, TypedDict


Decision = Literal["approved", "edited", "rejected"]


class DraftApproveState(TypedDict, total=False):
    """State for the draft-and-approve workflow (the canonical rung-2 loop).

    The assistant does the mechanical labor (retrieve context, draft); the human
    makes the judgment call (approve / edit / reject). That division is the whole
    point of human-in-the-loop, and it's a first-class part of the graph shape
    rather than bolted on.
    """

    # --- inputs (overwrite) ---
    user_id: str
    domain: str                      # "mail" | "chat" | "slack" (maps to autonomy.Domain)
    action: str                      # e.g. "draft_reply" (maps to autonomy.Action)
    incoming_ref: str                # pointer to the source item (e.g. the Gmail
                                     # thread id — what apply materializes
                                     # against); NOT the raw body
    incoming_summary: str            # short, provenance-tagged summary for the model
    sender: Optional[str]            # the thread's counterparty address (mail:
                                     # thread.from_addr; calendar: organizer, or
                                     # None) — feeds the per-sender importance
                                     # profile at capture time (Phase 1, G5)
    priority: Optional[str]          # effective triage.Priority value ("urgent" |
                                     # "routine" | "noise") that got this workflow
                                     # started (Phase 1, G4) — a seam for future
                                     # autonomy gating (Phase 4). The graph itself
                                     # does NOT branch on this today; only
                                     # dispatcher-level presentation (the urgent
                                     # card marker/notification) reads it.
    priority_adjusted: Optional[bool]  # whether the importance profile moved the
                                     # tier away from the model's own classification
                                     # (triage.TriageResult.adjusted)

    # --- working state (overwrite) ---
    retrieved_memories: list[str]    # preference/context snippets pulled pre-draft
    proposed_draft: Optional[str]    # what the assistant proposes
    final_text: Optional[str]        # what the human approved/edited (if any)
    decision: Optional[Decision]
    applied_ref: Optional[str]       # external ref apply produced (Gmail draft
                                     # id, or a calendar hold id)
    apply_error: Optional[str]       # exception class name if apply failed
    # Calendar hold proposals only (prompt 16): the exact slot the human is
    # approving rides in state as ISO strings — never parsed back out of the
    # proposal prose — so apply materializes precisely what the card showed.
    hold_start: Optional[str]
    hold_end: Optional[str]
    hold_summary: Optional[str]
    # Freshness precondition (prompt 21): what the source looked like when
    # this was proposed — mail: the thread's last_message_at ISO; calendar:
    # the conflicted event's start ISO. Apply refuses when it changed.
    source_snapshot: Optional[str]

    # --- accumulator: append-only, survives resume ---
    audit_events: Annotated[list[dict[str, Any]], operator.add]

    # --- guard against runaway loops (design/prod lesson) ---
    iteration_count: int
