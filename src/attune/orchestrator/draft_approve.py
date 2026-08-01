"""The draft-and-approve workflow (design doc 4.2) — the canonical rung-2 loop.

This graph is where the three primitives built in earlier phases finally meet:

    fuelix.model_for(Task.DRAFT)      -> which model drafts (routing)
    autonomy.PermissionMatrix         -> may we even act here, and at what rung
    memory.MemoryStore                -> search before drafting, capture after

Flow:
    retrieve -> draft -> [autonomy gate] -> approve(interrupt) -> apply -> capture

The apply step materializes an approved/edited decision into the real world —
for mail, a Gmail draft via ``connector.create_draft`` (the safe write path;
never send, rule 4). It runs through an injected ``apply_fn`` so the graph
stays free of connector imports; ``make_connector_apply_fn`` builds the real
one at assembly time (app.py/runtime.py). A rejected decision skips apply
entirely, and an apply failure is recorded honestly (``apply_error`` in state,
an ``apply_failed`` audit event) — the human's decision is never silently
dropped, and confirmations must never claim success that didn't happen.

The autonomy gate is the safety spine. Before a draft is ever shown for sending,
we check the permission matrix. If sending on this (action, domain) isn't granted
at rung ACT_NOTIFY or above, the graph *always* routes through the human approval
interrupt — it can never silently send. Only an explicit graduated grant lets a
workflow skip the interrupt, and that is a deliberate, per-(action,domain)
decision, never a global default.

LangGraph is imported lazily so the package imports without it; the graph is
built with injected collaborators (client, store, matrix) so it's testable
without a live LLM or a running Mem0.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

from ..llm import (
    ModelCapabilities,
    Task,
    call_kwargs,
    call_with_retry,
    create_chat_completion,
    model_for,
    read_usage,
    resolve_capabilities,
)
from ..memory.base import MemoryStore, Message
from ..memory.signals import (
    UNTRUSTED_FIELD_NOTE,
    UNTRUSTED_FIELD_OPEN,
    ActionSignal,
    capture_action_signal,
    capture_correction,
    frame_memory_text,
)
from ..prompts import PROMPT_DRAFT, render_system_message
from .autonomy import Action, Domain, PermissionMatrix, Rung, default_matrix
from .state import DraftApproveState

# Cap to prevent runaway conditional loops (a real production failure mode).
MAX_ITERATIONS = 10

# Hygiene/logistics actions (Phase 3 stage 1's LABEL; stage 2's
# DECLINE_INVITE/RESCHEDULE) -- see the `capture` node's docstring for the
# full rule this set backs. Kept as a set, not stacked booleans, so a
# future hygiene action is one line added here rather than a growing `or`
# chain wherever this distinction matters.
HYGIENE_ACTIONS = frozenset({
    Action.LABEL.value,
    Action.DECLINE_INVITE.value,
    Action.RESCHEDULE.value,
})


class SourceChangedError(Exception):
    """The source (thread/event) changed after the card was posted; a stale
    approval must not act on it (review finding #6)."""


def _accepts_kwarg(fn: Callable[..., Any], name: str) -> bool:
    """Whether ``fn`` (a possibly test-injected ``draft_fn``) declares
    ``name`` as a keyword parameter -- mirrors ``dispatcher._accepts_keyword``
    exactly, so the ``draft`` node below can pass ``capabilities``/
    ``usage_sink`` to :func:`_default_draft_fn` without breaking a custom
    ``draft_fn`` (``archive_draft_fn``, ``calendar_action_draft_fn``, or any
    test fake) that has never seen either keyword."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        p.name == name or p.kind == inspect.Parameter.VAR_KEYWORD for p in params
    )


def _audit(event: str, *, now: Any = None, **fields: Any) -> dict[str, Any]:
    """A single structured reason-for-action entry (design 4.7)."""
    from datetime import datetime, timezone

    return {
        "event": event,
        "ts": (now or datetime.now(timezone.utc)).isoformat(),
        **fields,
    }


def build_draft_approve_graph(
    *,
    client: Any,
    store: MemoryStore,
    matrix: PermissionMatrix | None = None,
    checkpointer: Any = None,
    draft_fn: Callable[[Any, str, list[str], str], str] | None = None,
    apply_fn: Callable[[dict[str, Any]], str | None] | None = None,
    matrix_provider: Callable[[], PermissionMatrix] | None = None,
    importance_profile: Any = None,
    min_score: float | None = None,
    capabilities: ModelCapabilities | None = None,
):
    """Compile the draft-and-approve graph.

    Args:
        client: an OpenAI-compatible chat client used by the
            default drafting function; injectable for tests.
        store: the MemoryStore to search before drafting and write signals after.
        matrix: permission matrix; defaults to the conservative default posture.
        checkpointer: a LangGraph checkpointer. REQUIRED for the approval
            interrupt to work; if None, an InMemorySaver is used (dev only).
        draft_fn: override the drafting call (tests inject a stub to avoid a
            live model).
        apply_fn: materializes an approved/edited decision (state -> external
            ref, e.g. a Gmail draft id, or None when there is nothing to
            materialize). Defaults to a no-op returning None; production binds
            ``make_connector_apply_fn(connector)`` at assembly time.
        matrix_provider: a zero-arg callable the GATE consults per
            evaluation (live policy — grants/revocations bite without a
            restart; see grants.make_matrix_provider). Wins over ``matrix``;
            absent, the static ``matrix`` is wrapped.
        importance_profile: an optional
            ``orchestrator.importance.ImportanceProfile``. When present, the
            capture node also records the human's decision against
            ``state["sender"]`` (Phase 1, G5/G6) — the same event that
            already feeds ``store`` via ``capture_action_signal``, so
            learning stays one behavior with two stores. Absent, or with no
            ``sender`` in state, capture behaves exactly as before.
        min_score: relevance floor passed to ``store.search`` (see
            ``Settings.memory_min_score``); ``None`` applies no floor.
        capabilities: build prompt 28's resolved gateway capabilities,
            passed to ``draft_fn`` when it declares a ``capabilities``
            keyword (:func:`_default_draft_fn` does); ``None`` (the
            default) resolves fresh from ``Settings.from_env()`` inside
            ``_default_draft_fn`` itself, the same lazy pattern
            ``llm.model_for`` already uses.
    """
    try:
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import Command, interrupt
        from langgraph.checkpoint.memory import InMemorySaver
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "The orchestrator requires langgraph. `pip install langgraph` "
            "before building graphs."
        ) from exc

    if matrix_provider is None:
        _static = matrix or default_matrix()
        matrix_provider = lambda: _static  # noqa: E731
    checkpointer = checkpointer or InMemorySaver()
    draft_fn = draft_fn or _default_draft_fn
    apply_fn = apply_fn or _noop_apply_fn

    def retrieve(state: DraftApproveState) -> dict[str, Any]:
        """Pull relevant preferences/context before drafting.

        Each snippet is provenance-framed (security finding F6, SEC-605)
        before it ever reaches the draft prompt: a memory captured from an
        edited-then-approved draft touched whatever the incoming mail/chat
        said, so it's annotated as lower-confidence than something the
        principal explicitly taught. Presentation only — retrieval and
        ranking are unaffected; see ``memory.signals.frame_memory_text``.

        The embedding query is ``state["retrieval_query"]`` when the caller
        supplied one (mail: a bounded subject+sender+lead-of-body string —
        see ``dispatcher.submit_gmail_thread`` — so a long thread body never
        becomes the query), falling back to ``incoming_summary`` for callers
        that already pass something short (calendar holds/reschedules,
        label/archive proposals).
        """
        query = state.get("retrieval_query") or state["incoming_summary"]
        mems = store.search(
            query,
            user_id=state["user_id"],
            limit=3,
            min_score=min_score,
        )
        snippets = [
            frame_memory_text(m.text, getattr(m, "metadata", None)) for m in mems
        ]
        # Build prompt 26's context_attribution: the whole point of the
        # decision ledger is knowing WHICH memory records were in context, not
        # just how many. Carried in the same order as ``snippets`` so a
        # future per-bullet accounting pass (prompt 29) can zip them back
        # together if it ever needs to.
        memory_ids = [m.id for m in mems]
        return {
            "retrieved_memories": snippets,
            "retrieved_memory_ids": memory_ids,
            "iteration_count": state.get("iteration_count", 0) + 1,
            "audit_events": [_audit("retrieved", count=len(snippets))],
        }

    def draft(state: DraftApproveState) -> dict[str, Any]:
        """Produce a proposed draft, conditioned on retrieved memories.

        ``prompt_version`` (build prompt 28) is recorded unconditionally,
        the same posture ``model=model_for(Task.DRAFT)`` already held before
        this change: every ``draft_fn`` compiled onto this graph -- the
        default model-calling one, or a fixed deterministic one like
        ``archive_draft_fn``/``calendar_action_draft_fn`` -- shares this one
        node. Token usage/cache-hit are only ever populated when ``draft_fn``
        actually calls a model AND declares the ``usage_sink`` keyword (see
        :func:`_accepts_kwarg`) -- a fixed draft_fn never sets them.
        """
        extra_kwargs: dict[str, Any] = {}
        if _accepts_kwarg(draft_fn, "capabilities"):
            extra_kwargs["capabilities"] = capabilities
        usage_box: list[Any] = []
        if _accepts_kwarg(draft_fn, "usage_sink"):
            extra_kwargs["usage_sink"] = usage_box.append
        text = draft_fn(
            client,
            state["incoming_summary"],
            state.get("retrieved_memories", []),
            state["domain"],
            **extra_kwargs,
        )
        fields: dict[str, Any] = {
            "model": model_for(Task.DRAFT),
            "chars": len(text),
            "prompt_version": PROMPT_DRAFT.version,
        }
        usage = usage_box[0] if usage_box else None
        if usage is not None:
            fields["input_tokens"] = usage.input_tokens
            fields["output_tokens"] = usage.output_tokens
            if usage.cache_hit is not None:
                fields["cache_hit"] = usage.cache_hit
        return {
            "proposed_draft": text,
            "audit_events": [_audit("drafted", **fields)],
        }

    def gate(state: DraftApproveState):
        """Autonomy gate: decide whether a human must approve before sending.

        Routes to 'approve' unless an explicit grant permits autonomous action
        on this (action, domain, context). This is the one place that
        authorizes skipping human review, and it fails safe.

        The evaluation context (Phase 4 stage 1, G14) is built from state,
        never inferred: ``priority`` is whatever Phase 1 triage put there
        (absent -> None), and ``tier`` is
        ``importance_profile.assess(sender).tier.value`` -- but ONLY when
        both an ``importance_profile`` and a ``sender`` are actually
        available; missing either leaves ``tier`` at None rather than
        guessing, and an assessment failure is swallowed the same way
        (fail-closed: a scoped grant that needs a signal we don't have never
        matches, so the gate falls back to whatever unscoped grant exists, or
        the READ_ONLY floor). The matrix does the actual scope-matching and
        urgent-interrupt work (autonomy.PermissionMatrix.max_rung) -- this
        node only assembles the context and reports it."""
        action = _as_action(state["action"])
        domain = _as_domain(state["domain"])
        current = matrix_provider()  # live policy: re-evaluated per gate
        priority = state.get("priority")
        sender = state.get("sender")
        tier: str | None = None
        profile_reason: str | None = None
        if importance_profile is not None and sender:
            try:
                assessment = importance_profile.assess(sender)
                tier = assessment.tier.value
                profile_reason = assessment.reason
            except Exception:  # noqa: BLE001 — an assessment failure must
                # never surface as a crash here; it just means this context
                # can't be enriched, and fail-closed scope matching already
                # treats a missing tier as "no scoped grant applies."
                tier = None
                profile_reason = None
        matched_rung = current.max_rung(action, domain, priority=priority, tier=tier)
        # Build prompt 26 (the decision ledger): the rung this grant would
        # confer BEFORE the urgent-interrupt downgrade, contrasted with
        # ``matched_rung`` (the capped, actually-used rung) to compute the
        # ledger's escalation-rate metric. Never used for routing —
        # ``matched_rung`` (below) still decides auto_apply vs. approve.
        granted_rung = current.max_rung(
            action, domain, priority=priority, tier=tier, ignore_urgent_cap=True
        )
        scope_matched = current.scope_matched(action, domain, priority=priority, tier=tier)
        autonomous_ok = Rung.ACT_NOTIFY <= matched_rung
        target = "auto_apply" if autonomous_ok else "approve"
        return Command(
            goto=target,
            update={
                "audit_events": [
                    _audit(
                        "autonomy_gate",
                        action=action.value,
                        domain=domain.value,
                        max_rung=int(matched_rung),
                        routed_to=target,
                        scope_context={
                            "priority": priority,
                            "tier": tier,
                            "matched_rung": int(matched_rung),
                        },
                        autonomy_rung_granted=int(granted_rung),
                        scope_matched=scope_matched,
                        profile_reason=profile_reason,
                    )
                ]
            },
        )

    def approve(state: DraftApproveState) -> dict[str, Any]:
        """Pause for human judgment. The graph freezes here until resumed with
        Command(resume={'decision': ..., 'text': ...})."""
        response = interrupt(
            {
                "question": "Approve this draft?",
                "domain": state["domain"],
                "proposed_draft": state.get("proposed_draft"),
                "why": state.get("retrieved_memories", []),
            }
        )
        decision = (response or {}).get("decision", "rejected")
        edited_text = (response or {}).get("text")
        final = edited_text if decision == "edited" else state.get("proposed_draft")
        return {
            "decision": decision,
            "final_text": final if decision != "rejected" else None,
            "audit_events": [_audit("human_decision", decision=decision)],
        }

    def auto_apply(state: DraftApproveState) -> dict[str, Any]:
        """Autonomous path (only reached when explicitly granted)."""
        return {
            "decision": "approved",
            "final_text": state.get("proposed_draft"),
            "audit_events": [_audit("auto_applied")],
        }

    def apply(state: DraftApproveState) -> dict[str, Any]:
        """Materialize the human's (or auto_apply's) decision — the step that
        turns "approved" into an actual Gmail draft rather than a dead end.

        Never raises: an apply failure must not lose the decision or the
        capture step that follows; it's recorded in state (``apply_error``)
        and the audit trail so the channel can report it honestly."""
        decision = state.get("decision")
        if decision not in ("approved", "edited") or not state.get("final_text"):
            return {
                "applied_ref": None,
                "audit_events": [
                    _audit("apply_skipped", reason=decision or "no_decision")
                ],
            }
        try:
            ref = apply_fn(dict(state))
        except SourceChangedError as exc:
            return {
                "applied_ref": None,
                "apply_error": "source_changed",
                "audit_events": [
                    _audit("apply_skipped", reason="source_changed",
                           detail=str(exc))
                ],
            }
        except Exception as exc:  # noqa: BLE001 — honesty over crash, see docstring
            return {
                "applied_ref": None,
                "apply_error": type(exc).__name__,
                "audit_events": [
                    _audit("apply_failed", error=type(exc).__name__)
                ],
            }
        if ref is None:
            return {
                "applied_ref": None,
                "audit_events": [
                    _audit("apply_skipped", reason="nothing_to_materialize")
                ],
            }
        return {"applied_ref": ref, "audit_events": [_audit("applied", ref=ref)]}

    def capture(state: DraftApproveState) -> dict[str, Any]:
        """Write the learning signal from what the human did (design 2.2).

        Approval-signal rule (Phase 3 stages 1-2; the ONE place this rule is
        stated): only DRAFT_REPLY and FOLLOW_UP approvals feed the sender's
        importance profile as positive engagement. Every action in
        ``HYGIENE_ACTIONS`` (LABEL, DECLINE_INVITE, RESCHEDULE) is a
        hygiene/logistics judgment, never counterpart engagement -- see the
        block below for why. CREATE_HOLD isn't in that set (it carries no
        ``sender`` at all today), but reaches the same outcome because
        ``capture_action_signal`` is a no-op without one.
        """
        decision = state.get("decision")
        domain = state["domain"]
        uid = state["user_id"]
        if decision == "edited" and state.get("final_text"):
            # Build prompt 25, task 3: capture_correction used to be called
            # with no context at all, so its extraction prompt rendered
            # "Context: n/a" and every learned preference was scoped to the
            # bare string "mail" — indistinguishable from a preference about
            # a completely different counterparty. state["sender"]/
            # state["subject"] (both now populated by every caller, see
            # task 1) are exactly the counterparty/topic this needed.
            context_bits = [b for b in (state.get("sender"), state.get("subject")) if b]
            capture_correction(
                store,
                user_id=uid,
                domain=domain,
                proposed=state.get("proposed_draft") or "",
                sent=state["final_text"],
                context=" — ".join(context_bits) or None,
            )
            sig = ActionSignal.EDITED
        elif decision == "approved":
            sig = ActionSignal.APPROVED
        else:
            sig = ActionSignal.REJECTED

        # Hygiene-action asymmetry (Phase 3 stage 1, G9; generalized in
        # stage 2 -- see docs/decisions.md): everywhere else in this graph,
        # "approved" means the assistant's judgment was right, so the
        # dual-write feeds that back into the sender's importance profile as
        # positive engagement (Phase 1). A hygiene-action capture means the
        # OPPOSITE for the sender/organizer: approving an archive proposal
        # says "yes, this sender is noise"; approving a decline/reschedule
        # says "yes, deprioritize this organizer's meeting," not "engage
        # with them more." Feeding either through as APPROVED would push
        # that party's tier toward HIGH -- exactly backwards. So hygiene
        # captures still write the raw signal to memory (ground truth for
        # nightly consolidation, with the same metadata every capture gets)
        # but never touch the importance profile; the profile only learns
        # about a sender through the mechanism that actually observed them
        # (NOISE triage for LABEL; ordinary DRAFT_REPLY/FOLLOW_UP approvals
        # otherwise), not through a hygiene approval.
        is_hygiene_action = state.get("action") in HYGIENE_ACTIONS
        capture_action_signal(
            store,
            user_id=uid,
            domain=domain,
            signal=sig,
            summary=f"{state['action']} on {domain}",
            metadata={"hygiene_action": True} if is_hygiene_action else None,
            importance_profile=None if is_hygiene_action else importance_profile,
            # Build prompt 25, task 1: sender/subject/priority enrich the
            # captured text/meta regardless of the hygiene asymmetry above —
            # that asymmetry is specifically about NOT feeding the
            # importance PROFILE for a hygiene approval (suppressed via
            # importance_profile=None above), not about withholding
            # discriminating text from ground truth. Every capture, hygiene
            # or not, is equally retrievable ground truth for consolidation.
            sender=state.get("sender"),
            subject=state.get("subject"),
            priority=state.get("priority"),
        )
        return {"audit_events": [_audit("signal_captured", signal=sig.value)]}

    g = StateGraph(DraftApproveState)
    g.add_node("retrieve", retrieve)
    g.add_node("draft", draft)
    g.add_node("gate", gate)
    g.add_node("approve", approve)
    g.add_node("auto_apply", auto_apply)
    g.add_node("apply", apply)
    g.add_node("capture", capture)

    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "draft")
    g.add_edge("draft", "gate")
    # gate uses Command(goto=...) for dynamic routing to approve|auto_apply
    g.add_edge("approve", "apply")
    g.add_edge("auto_apply", "apply")
    g.add_edge("apply", "capture")
    g.add_edge("capture", END)

    return g.compile(checkpointer=checkpointer)


# The events a resume produces (vs. the pre-interrupt events the dispatcher
# already recorded at proposal time). Auto-applied runs never resume, so
# name-filtering here cannot double-record anything.
POST_RESUME_EVENTS = frozenset({
    "human_decision", "applied", "apply_skipped", "apply_failed",
    "signal_captured",
})


def resume_workflow(
    graph: Any,
    thread_id: str,
    decision: str,
    text: str | None = None,
    *,
    pending: Any = None,
    audit_log: Any = None,
    user_id: str | None = None,
    actor: str | None = None,
    ledger: Any = None,
) -> Any:
    """Resume a paused draft-approve workflow with a human decision.

    One implementation of the ``Command(resume=...)`` invoke, shared by every
    channel's button-click handling (Slack, Chat) and by the async Chat
    card-interaction path (``dispatcher.handle_chat_interaction``) — this used
    to be duplicated per channel; a third caller was the point where that
    stopped being worth it.

    ``pending`` is an optional :class:`~orchestrator.pending.PendingApprovals`
    registry; being the single resume path makes this the one place every
    decision marks its card resolved (so the ignore-sweep never fires on an
    answered card). ``resolve`` is a no-op for workflows never registered.

    ``audit_log`` (with ``user_id``/``actor``) records the POST-RESUME events
    (review finding #4: they used to live only in the checkpoint, so the
    graduation track record could never see a real human decision). The
    domain comes from the result state — never hardcoded per channel — and
    ``actor`` (who clicked, prompt 17) is stamped onto ``human_decision``.
    Audit failures never break a resume (best-effort, logged).

    ``ledger`` (build prompt 26, ``orchestrator.ledger.DecisionLedger``),
    when supplied, completes the decision-ledger row here — the single
    shared resume path is exactly where every action type's decision-time
    fields (decision, edit metrics, applied_ok, ...) become known at once,
    regardless of which dispatcher call site proposed it. Best-effort, same
    posture as ``audit_log`` above.
    """
    from langgraph.types import Command

    if pending is not None and hasattr(pending, "claim"):
        claimed = pending.claim(thread_id, actor=actor)
        if claimed is False:
            return {
                "decision": decision,
                "apply_error": "already_handled",
                "approval_already_handled": True,
            }

    cfg = {"configurable": {"thread_id": thread_id}}
    payload: dict[str, Any] = {"decision": decision}
    if text is not None:
        payload["text"] = text
    result = graph.invoke(Command(resume=payload), cfg)
    if pending is not None and not hasattr(pending, "claim"):
        pending.resolve(thread_id)

    if audit_log is not None and isinstance(result, dict):
        events = []
        for event in result.get("audit_events", []):
            if event.get("event") not in POST_RESUME_EVENTS:
                continue
            enriched = dict(event)
            if enriched["event"] == "human_decision" and actor:
                enriched["actor"] = actor
            events.append(enriched)
        if events:
            try:
                audit_log.record(
                    thread_id=thread_id,
                    workflow="draft_approve",
                    events=events,
                    domain=result.get("domain"),
                    user_id=user_id,
                )
            except Exception:  # noqa: BLE001 — audit must never break a resume
                import logging

                logging.getLogger(__name__).warning(
                    "resume audit record failed for %s", thread_id, exc_info=True
                )

    if ledger is not None and isinstance(result, dict):
        from .ledger import record_decision

        record_decision(ledger, thread_id=thread_id, result=result, actor=actor)

    return result


def make_connector_apply_fn(
    connector: Any, *, owner_email: str | None = None
) -> Callable[[dict[str, Any]], str | None]:
    """Build the production ``apply_fn``: materialize a mail decision as a
    Gmail draft via ``connector.create_draft`` (the safe write path — never
    send, rule 4; the human sends from Gmail).

    ``connector`` is duck-typed (needs ``get_thread``/``create_draft``) so this
    module never imports the connector layer. The graph state's
    ``incoming_ref`` is the Gmail thread id (set by
    ``dispatcher.handle_gmail_notification``); the recipient and subject are
    re-fetched from the thread rather than carried in checkpoint state, per
    the state discipline (pointers, not payloads).

    Recipient resolution (review finding #3): the thread's ``reply_to`` —
    the newest counterparty message's Reply-To/From — then
    ``last_from_addr``, then ``from_addr``. A recipient that is empty or is
    the owner refuses to materialize: the assistant never drafts to its own
    principal.

    SEND_REPLY (Phase 4 stage 2, G15): when ``state["action"] ==
    Action.SEND_REPLY.value``, the draft is created exactly as above and
    THEN immediately sent via ``connector.send_reply(draft_id=...)`` — the
    freshness check above already covers this apply call (there is no
    separate window between drafting and sending for a stale thread to
    slip through). ``connector.send_reply`` is itself structurally gated
    (rule 4: ``send_enabled`` + a real gmail.send scope); this call site
    adds no shortcut around that — a connector built without send actually
    enabled still raises ``SendNotPermitted`` here, which the ``apply``
    node's exception handling turns into an honest ``apply_failed`` event
    rather than a silent send.
    """

    def apply(state: dict[str, Any]) -> str | None:
        if state.get("domain") == "calendar":
            return _apply_calendar_hold(connector, state)
        if state.get("domain") != "mail":
            return None
        thread_ref = state.get("incoming_ref")
        final_text = state.get("final_text")
        if not thread_ref or not final_text:
            return None
        thread = connector.get_thread(thread_ref)
        _check_freshness_mail(thread, state.get("source_snapshot"))
        to = (
            getattr(thread, "reply_to", "")
            or getattr(thread, "last_from_addr", "")
            or thread.from_addr
        )
        if not to or (owner_email and owner_email.lower() in to.lower()):
            return None  # nobody to draft to, or it would be the owner
        subject = thread.subject or ""
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}" if subject else "Re:"
        ref = connector.create_draft(
            to=to,
            subject=subject,
            body=final_text,
            thread_id=thread_ref,
        )
        draft_id = getattr(ref, "draft_id", None)
        if state.get("action") == Action.SEND_REPLY.value and draft_id:
            connector.send_reply(draft_id=draft_id)
        return draft_id

    return apply


def archive_draft_fn(
    client: Any, incoming_summary: str, memories: list[str], domain: str
) -> str:
    """The archive proposal's fixed, deterministic ``draft_fn`` (Phase 3
    stage 1, G9): the thread was already classified NOISE by triage, so
    there is nothing left for a model to draft — ``incoming_summary`` IS the
    final proposal text (built once, in ``dispatcher._archive_proposal_text``,
    and carried verbatim through graph state). This function's only job is
    to hand it back unchanged: ``build_draft_approve_graph``'s ``draft`` node
    still runs (it's the state transition that produces ``proposed_draft``,
    and the shape every downstream node/audit event expects), but it never
    calls the model, exactly as the design calls for ("no model call — the
    thread was already classified"). ``client``/``memories``/``domain`` are
    accepted for signature-compatibility with every other ``draft_fn`` and
    are intentionally unused.

    Compiled into its OWN graph instance (``AppContext.label_graph``), never
    into the shared draft-reply/follow-up/hold graph: ``domain`` alone can't
    tell this function apart from a real DRAFT_REPLY on ``domain="mail"``,
    so a fixed draft_fn has to live on a separate compiled graph rather than
    branch inside the shared one."""
    return incoming_summary


def make_label_apply_fn(connector: Any) -> Callable[[dict[str, Any]], str | None]:
    """Build the production ``apply_fn`` for archive proposals (Phase 3
    stage 1, G9): materialize an approved decision via
    ``connector.label_thread`` — never ``create_draft``. Compiled into the
    dedicated label graph instance (see :func:`archive_draft_fn`), alongside
    ``make_connector_apply_fn`` for the shared draft/hold graph.

    Mirrors ``make_connector_apply_fn``'s freshness discipline (a thread that
    gained messages since the card was posted is stale — see
    ``_check_freshness_mail``) but the whole effect IS the label/archive;
    there is no draft artifact to point at, so ``applied_ref`` is the
    thread id itself (enough for ``apply_confirmation``/audit to report
    something concrete happened).
    """

    def apply(state: dict[str, Any]) -> str | None:
        if state.get("action") != Action.LABEL.value:
            return None
        thread_ref = state.get("incoming_ref")
        label_name = state.get("label_name")
        if not thread_ref or not label_name:
            return None
        thread = connector.get_thread(thread_ref)
        _check_freshness_mail(thread, state.get("source_snapshot"))
        connector.label_thread(thread_ref, label=label_name, archive=True)
        return thread_ref

    return apply


def calendar_action_draft_fn(
    client: Any, incoming_summary: str, memories: list[str], domain: str
) -> str:
    """The DECLINE_INVITE/RESCHEDULE proposal's fixed, deterministic
    ``draft_fn`` (Phase 3 stage 2): identical shape to
    :func:`archive_draft_fn` and for the same reason -- the dispatcher
    already computed the deterministic reason/slot before ever invoking this
    graph, so there is nothing left for a model to draft.
    ``incoming_summary`` IS the final proposal text (built once, in
    ``dispatcher._offer_decline_proposal``/``_offer_reschedule_proposal``,
    and carried verbatim through graph state). Kept as its own function
    (not an alias of ``archive_draft_fn``) so the two proposal families'
    docstrings can diverge independently later; ``client``/``memories``/
    ``domain`` are accepted for signature-compatibility and unused.

    Compiled into its OWN graph instance (``AppContext.calendar_action_graph``),
    for the same reason ``archive_draft_fn`` needed one: ``domain="calendar"``
    alone can't tell a DECLINE_INVITE/RESCHEDULE proposal apart from a real
    CREATE_HOLD proposal (which DOES call a model to draft the reschedule-
    request message -- see ``_default_draft_fn`` on the shared graph)."""
    return incoming_summary


def make_calendar_action_apply_fn(
    connector: Any,
) -> Callable[[dict[str, Any]], str | None]:
    """Build the production ``apply_fn`` for DECLINE_INVITE/RESCHEDULE
    proposals (Phase 3 stage 2): materializes via ``connector.decline_invite``
    or ``connector.reschedule_event`` -- never ``create_draft``. Compiled
    into the dedicated calendar-action graph instance (see
    :func:`calendar_action_draft_fn`), branching on ``state["action"]`` the
    same way ``make_connector_apply_fn`` branches on ``state["domain"]`` for
    CREATE_HOLD.

    Freshness discipline (mirrors ``_check_freshness_mail``/
    ``_apply_calendar_hold``): a FRESH ``connector.get_event`` fetch backs
    every check here -- the event's start time is unchanged since the card
    was posted, and (decline only) it's still ``needsAction``. For
    RESCHEDULE, the organizer re-verification happens a second time, inside
    ``connector.reschedule_event`` itself, from ITS OWN fresh fetch -- this
    function never trusts anything about organizer identity from state.
    """

    def apply(state: dict[str, Any]) -> str | None:
        from datetime import datetime

        action = state.get("action")
        event_ref = state.get("incoming_ref")
        if not event_ref:
            return None

        if action == Action.DECLINE_INVITE.value:
            current = connector.get_event(event_ref)
            _check_freshness_calendar_event(current, state.get("source_snapshot"))
            if current.response_status != "needsAction":
                raise SourceChangedError(
                    f"event {event_ref} is no longer needsAction "
                    f"(now {current.response_status!r})"
                )
            connector.decline_invite(event_ref)
            return event_ref

        if action == Action.RESCHEDULE.value:
            start_raw = state.get("reschedule_start")
            end_raw = state.get("reschedule_end")
            if not start_raw or not end_raw:
                return None
            current = connector.get_event(event_ref)
            _check_freshness_calendar_event(current, state.get("source_snapshot"))
            connector.reschedule_event(
                event_ref,
                new_start=datetime.fromisoformat(start_raw),
                new_end=datetime.fromisoformat(end_raw),
            )
            return event_ref

        return None

    return apply


def _check_freshness_calendar_event(event: Any, snapshot: str | None) -> None:
    """Shared freshness precondition for DECLINE_INVITE/RESCHEDULE apply: the
    event's start time must be unchanged since the card was posted (mirrors
    ``_apply_calendar_hold``'s snapshot check). Older cards carry no
    snapshot; proceed (back-compat), same posture as
    ``_check_freshness_mail``."""
    if not snapshot:
        return
    if event.start.isoformat() != snapshot:
        raise SourceChangedError(
            f"event {event.event_id} start changed from {snapshot} to "
            f"{event.start.isoformat()} since the card was posted"
        )


def _apply_calendar_hold(connector: Any, state: dict[str, Any]) -> str | None:
    """Materialize an approved hold proposal: a NEW tentative event at the
    exact slot carried in state (never re-derived from the proposal prose),
    no attendees invited — the reversible, external-attendee-free shape the
    calendar-actions decision allows (docs/decisions.md, prompt 16)."""
    from datetime import datetime
    from types import SimpleNamespace

    start_raw, end_raw = state.get("hold_start"), state.get("hold_end")
    if not start_raw or not end_raw:
        return None
    snapshot = state.get("source_snapshot")
    source_ref = state.get("incoming_ref")
    if snapshot and source_ref:
        current = connector.get_event(source_ref)
        if current.start.isoformat() != snapshot:
            raise SourceChangedError(
                f"event {source_ref} moved from {snapshot} to "
                f"{current.start.isoformat()} since the card was posted"
            )
    hold = SimpleNamespace(
        event_id="",
        summary=state.get("hold_summary") or "HOLD",
        start=datetime.fromisoformat(start_raw),
        end=datetime.fromisoformat(end_raw),
        attendees=[],
        external_attendees=False,
    )
    return connector.create_hold(hold)


def _check_freshness_mail(thread: Any, snapshot: str | None) -> None:
    """A thread that gained messages after the card was posted is stale —
    the human approved a reply to a conversation that has since moved on."""
    if not snapshot:
        return  # older cards carry no snapshot; proceed (back-compat)
    last = getattr(thread, "last_message_at", None)
    if last is not None and last.isoformat() > snapshot:
        raise SourceChangedError(
            f"thread changed at {last.isoformat()} (card snapshot {snapshot})"
        )


def _noop_apply_fn(state: dict[str, Any]) -> str | None:
    """Default apply: nothing to materialize (dev/tests without a connector)."""
    return None


def apply_confirmation(decision: str, result: Any = None) -> str:
    """The honest post-decision confirmation, shared by every channel.

    ``result`` is whatever ``resume_workflow`` (or, for a graduation/
    demotion card, ``orchestrator.grants.resolve_autonomy_card`` — Phase 4
    stage 2, G13) returned. The text states only what actually happened: a
    created Gmail draft is announced, an apply failure is admitted, a SENT
    reply is announced as sent (never claimed for anything that wasn't
    actually granted+enabled — rule 4), and an autonomy card's resolution
    (granted/rejected/refused) gets its own honest rendering.
    """
    state = result if isinstance(result, dict) else {}
    if state.get("card_kind") == "autonomy":
        return _autonomy_card_confirmation(decision, state)

    is_send = state.get("action") == Action.SEND_REPLY.value

    if decision == "rejected":
        return "🗑️ Rejected — nothing sent." if not is_send else "🗑️ Rejected — reply not sent."

    prefix = "✏️ Edited" if decision == "edited" else "✅ Approved"
    if state.get("approval_already_handled"):
        return "This approval was already handled; no second action was taken."
    is_calendar = state.get("domain") == "calendar"
    thing = "reply" if is_send else ("tentative calendar hold" if is_calendar else "Gmail draft")
    if state.get("apply_error") == "source_changed":
        source = "meeting" if is_calendar else "thread"
        return (
            f"{prefix} — but the {source} changed since this card was "
            f"posted, so nothing was created. Please re-review."
        )
    if state.get("apply_error"):
        verb = "sending" if is_send else "creating"
        return (
            f"{prefix} — your decision was recorded, but {verb} the "
            f"{thing} failed ({state['apply_error']})."
        )
    if state.get("applied_ref"):
        if is_send:
            return f"{prefix} — reply sent."
        return (
            f"{prefix} — tentative hold created on your calendar."
            if is_calendar
            else f"{prefix} — draft created in Gmail."
        )
    return f"{prefix}."


def _autonomy_card_confirmation(decision: str, state: dict[str, Any]) -> str:
    """The honest post-decision confirmation for a graduation/demotion
    approval card (Phase 4 stage 2, G13) — the counterpart to the rest of
    ``apply_confirmation`` for cards that resolve through
    ``orchestrator.grants.resolve_autonomy_card`` rather than a LangGraph
    resume. Every branch states exactly what did or didn't happen; nothing
    here can claim a grant took effect when the ceiling refused it."""
    resolution = state.get("resolution")
    if resolution == "already_handled":
        return "This approval was already handled; no second action was taken."
    if resolution == "unknown_card":
        return "🗑️ This suggestion has expired or was already resolved — nothing changed."
    if resolution == "refused_ceiling":
        return (
            "⛔ Refused — this suggestion exceeds what a card may ever grant "
            "(sending, or autonomy above act-notify, is CLI-only). Nothing "
            "changed; use `attune autonomy grant` if this is truly wanted."
        )
    if resolution == "granted":
        return (
            f"✅ Granted — {state.get('action')} on {state.get('domain')} at "
            f"{state.get('to_rung')} (persisted; revoke any time with "
            f"`attune autonomy revoke {state.get('action')} {state.get('domain')}`)."
        )
    if resolution == "rejected":
        return (
            f"🗑️ Rejected — {state.get('action')} on {state.get('domain')} "
            "stays as-is; this suggestion won't reappear for 30 days."
        )
    return "Handled."


def _default_draft_fn(
    client: Any,
    incoming_summary: str,
    memories: list[str],
    domain: str,
    *,
    capabilities: ModelCapabilities | None = None,
    usage_sink: Callable[[Any], None] | None = None,
) -> str:
    """Default drafting: one Chat Completions call routed to the DRAFT model.

    Memories are injected as guidance. The incoming content is presented as
    UNTRUSTED — the provenance discipline from the design's security section is
    enforced right at the prompt boundary.

    ``capabilities``/``usage_sink`` are new, keyword-only, additive
    parameters (build prompt 28): ``capabilities`` resolves fresh from
    ``Settings.from_env()`` when omitted, and ``usage_sink`` — when the
    ``draft`` node above detects this function declares it — receives the
    call's :class:`llm.Usage` so the ``drafted`` audit event can report
    token counts and cache hit/miss. Neither changes this function's return
    value or its behavior for a caller that doesn't pass them.
    """
    caps = capabilities or resolve_capabilities()
    mem_block = "\n".join(f"- {m}" for m in memories) or "(no prior preferences)"
    volatile = "Learned preferences:\n" + mem_block
    if UNTRUSTED_FIELD_OPEN in mem_block:
        # A captured action signal's sender/subject is attacker-influenced
        # (build prompt 25, task 1) even though the capture event itself is
        # trusted ground truth — same provenance discipline as triage's
        # PAST REACTIONS block.
        volatile += "\n\n" + UNTRUSTED_FIELD_NOTE
    resp = call_with_retry(
        lambda: create_chat_completion(
            client,
            model=model_for(Task.DRAFT),
            messages=[
                render_system_message(PROMPT_DRAFT.stable_prefix, volatile, capabilities=caps),
                {"role": "user", "content": f"[UNTRUSTED {domain}]\n{incoming_summary}"},
            ],
            **call_kwargs(caps),
        ),
        capabilities=caps,
    )
    if usage_sink is not None:
        usage_sink(read_usage(resp))
    return resp.choices[0].message.content


def _as_action(value: str) -> Action:
    try:
        return Action(value)
    except ValueError:
        return Action.DRAFT_REPLY


def _as_domain(value: str) -> Domain:
    try:
        return Domain(value)
    except ValueError:
        return Domain.MAIL
