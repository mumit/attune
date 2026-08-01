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
    retrieval_query: Optional[str]   # short text embedded for the retrieve node's
                                     # memory search; when absent, retrieve() falls
                                     # back to incoming_summary (already short for
                                     # every non-mail caller). Mail callers pass a
                                     # bounded subject+sender+lead-of-body query so
                                     # a long thread body never becomes the query.
    sender: Optional[str]            # the thread's counterparty address (mail:
                                     # thread.from_addr; calendar: organizer, or
                                     # None) — feeds the per-sender importance
                                     # profile at capture time (Phase 1, G5)
    subject: Optional[str]           # thread.subject / event.summary — the
                                     # discriminating topic feeding the
                                     # capture node's signal text (build
                                     # prompt 25, task 1). Attacker-influenced
                                     # (a Subject header); capture_action_signal
                                     # fences it before it ever reaches a
                                     # prompt.
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
    base_priority: Optional[str]     # triage.TriageResult.base_priority — what the
                                     # model itself classified, before any importance-
                                     # profile adjustment (build prompt 26, the
                                     # decision ledger's base_priority field).

    # --- working state (overwrite) ---
    retrieved_memories: list[str]    # preference/context snippets pulled pre-draft
    retrieved_memory_ids: list[str]  # the MemoryRecord.id of each snippet above, in
                                     # the same order (build prompt 26's
                                     # context_attribution — this is the whole
                                     # point of the decision ledger: without these
                                     # ids, no learning mechanism can ever credit
                                     # or blame a specific memory record).
    playbook_bullet_ids: list[str]   # build prompt 29: the ids of every playbook
                                     # bullet actually shown to the model for
                                     # this domain's slice — feeds
                                     # context_attribution.playbook_bullet_ids
                                     # (ledger.py) so the nightly reflector can
                                     # credit/blame a specific learned rule.
                                     # Absent (no playbook collaborator wired)
                                     # is exactly today's behavior — empty.
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
    # RESCHEDULE proposals only (Phase 3 stage 2, Deliverable C): the exact
    # free slot the human is approving, same discipline as hold_start/
    # hold_end above -- carried as ISO strings, never re-derived from the
    # proposal prose, so apply moves the event to precisely what the card
    # showed.
    reschedule_start: Optional[str]
    reschedule_end: Optional[str]
    # RESCHEDULE undo (build prompt 31, task 1): the event's start/end AS
    # THEY WERE BEFORE the patch, captured at PROPOSE time (dispatcher.
    # _offer_reschedule_proposal) -- never re-derived after the fact, since
    # by the time a compensating action would run, the event's own start/
    # end IS the moved-to time, not the original. ``reschedule_start``/
    # ``reschedule_end`` above are the NEW (moved-to) time; these are the
    # OLD one RESCHEDULE's compensate function restores.
    reschedule_prior_start: Optional[str]
    reschedule_prior_end: Optional[str]
    # Freshness precondition (prompt 21): what the source looked like when
    # this was proposed — mail: the thread's last_message_at ISO; calendar:
    # the conflicted event's start ISO. Apply refuses when it changed.
    source_snapshot: Optional[str]
    # LABEL proposals only (Phase 3 stage 1, G9): the label to apply, e.g.
    # connectors.base.DEFAULT_NOISE_LABEL. Found missing during Phase 4
    # stage 2 while writing a REAL (not fake-graph) regression test for the
    # resume-routing fix: an undeclared TypedDict key is silently dropped
    # by LangGraph across the interrupt/resume boundary, so
    # make_label_apply_fn's apply() always saw label_name=None on a real
    # compiled graph and skipped ("nothing_to_materialize") — the archive-
    # proposal write path never actually archived anything in production.
    # See docs/decisions.md.
    label_name: Optional[str]

    # Build prompt 30: the gate's routing decision as a typed value, read
    # directly by dispatcher._auto_rung instead of string-matching over
    # audit_events for an "autonomy_gate" event with routed_to=="auto_apply".
    # None until the gate node runs; "approve" or "auto_apply" after.
    routed_to: Optional[str]
    # The matched (capped) rung the gate used to route, only meaningful
    # when routed_to == "auto_apply" — the same value the audit event's
    # max_rung field already carried, now also readable without a scan.
    gate_max_rung: Optional[int]

    # Build prompt 31: whether the applied effect can be undone — set by
    # the ``apply`` node from ``registry.get(action)``'s ``compensate``/
    # ``irreversible`` fields, so ``apply_confirmation`` can offer (or
    # correctly withhold, for SEND_REPLY) an undo affordance without
    # needing the registry itself threaded through every channel.
    undo_available: Optional[bool]

    # --- accumulator: append-only, survives resume ---
    audit_events: Annotated[list[dict[str, Any]], operator.add]
