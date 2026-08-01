"""The capability registry (build prompt 30) — one declarative descriptor
per action, replacing the 11-13-file ceremony adding a single Attune action
used to require: the ``Action`` enum entry, a connector probe, a
``*NotPermitted`` exception, a Google implementation, the MCP structural
refusal, a ``draft_fn``/``apply_fn``/``apply_confirmation`` branch, a
dedicated compiled graph plus an ``AppContext`` field plus a ``build_app``
kwarg, a rank/cap/gate function in ``dispatcher.py``, a resume-routing
branch in ``runtime.py``, a settings field, ``.env.example``, exports,
scopes, Doctor, and docs.

This module names what already existed for the six pre-prompt-30
capabilities (``DRAFT_REPLY``, ``SEND_REPLY``, ``LABEL``, ``CREATE_HOLD``,
``DECLINE_INVITE``, ``RESCHEDULE``) plus ``FOLLOW_UP`` — every ``propose``/
``apply`` callable below is the exact function that already backed that
capability; nothing about their behavior changes here. What's new is having
one place that says so, and a shape a NEW capability can fill in without
touching the graph, the dispatcher's gate logic, or runtime.py's resume
path at all.

``compensate``/``irreversible`` (build prompt 31, task 1): five capabilities
now carry a real compensating action — DRAFT_REPLY (delete the created
draft), LABEL (re-add INBOX, remove the applied label), CREATE_HOLD (delete
the created event), DECLINE_INVITE (reset responseStatus to needsAction),
RESCHEDULE (restore the prior start/end, captured pre-patch). SEND_REPLY
stays ``irreversible=True`` unconditionally — a sent reply is irreversible
by construction, and a follow-up "please ignore that" email is not an undo
(see docs/decisions.md). Every OTHER capability registered here (FOLLOW_UP,
ADD_LABEL, REMOVE_LABEL, MARK_READ, RSVP_ACCEPT, RSVP_TENTATIVE) is still
honestly ``compensate=None, irreversible=True`` — out of build prompt 31's
scope — and the constraint (never register a capability without one or the
other) keeps that gap visible instead of silently assumed away.
:mod:`orchestrator.undo` is what actually invokes ``compensate`` — this
module only declares it.

``render_card``/``rank``/``max_per_run`` are populated for capabilities
whose card text and ranking are already simple, self-contained functions of
state (the hygiene actions, and the capabilities added in this same build
prompt). For the four capabilities whose presentation is entangled with
call-site-specific context (the urgent marker, the SEND_REPLY title, nudge
day-counts, conflict ranking against a sibling CREATE_HOLD offer) those
fields stay ``None`` — the existing ``dispatcher.py``/``draft_approve.py``
logic they'd otherwise duplicate is unchanged and already covered by
passing tests. See ``docs/decisions.md`` for the record of this scoping.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .autonomy import Action, Domain, RiskTier, Rung
from .draft_approve import (
    SourceChangedError,
    _check_freshness_calendar_event,
    _check_freshness_mail,
    _noop_apply_fn,
    apply_confirmation,
    archive_draft_fn,
    calendar_action_draft_fn,
)
from .draft_approve import _default_draft_fn as _default_propose_fn


def make_add_label_apply_fn(connector: Any) -> "Callable[[dict], str | None]":
    """Build the production ``apply_fn`` for ADD_LABEL (build prompt 30,
    task 6.1) — materializes via ``connector.add_label``, mirroring
    ``draft_approve.make_label_apply_fn``'s shape (freshness check, read
    ``incoming_ref``/``label_name`` from state) but never archives."""

    def apply(state: dict) -> "str | None":
        if state.get("action") != Action.ADD_LABEL.value:
            return None
        thread_ref = state.get("incoming_ref")
        label_name = state.get("label_name")
        if not thread_ref or not label_name:
            return None
        thread = connector.get_thread(thread_ref)
        _check_freshness_mail(thread, state.get("source_snapshot"))
        connector.add_label(thread_id=thread_ref, label=label_name)
        return thread_ref

    return apply


def make_remove_label_apply_fn(connector: Any) -> "Callable[[dict], str | None]":
    """Build the production ``apply_fn`` for REMOVE_LABEL (build prompt 30,
    task 6.1) — materializes via ``connector.remove_label``, same shape as
    :func:`make_add_label_apply_fn`."""

    def apply(state: dict) -> "str | None":
        if state.get("action") != Action.REMOVE_LABEL.value:
            return None
        thread_ref = state.get("incoming_ref")
        label_name = state.get("label_name")
        if not thread_ref or not label_name:
            return None
        thread = connector.get_thread(thread_ref)
        _check_freshness_mail(thread, state.get("source_snapshot"))
        connector.remove_label(thread_ref, label=label_name)
        return thread_ref

    return apply


def make_mark_read_apply_fn(connector: Any) -> "Callable[[dict], str | None]":
    """Build the production ``apply_fn`` for MARK_READ (build prompt 30,
    task 6.1) — materializes via ``connector.mark_read``. Needs no
    ``label_name`` (unlike ADD_LABEL/REMOVE_LABEL) — there's exactly one
    label this capability ever touches."""

    def apply(state: dict) -> "str | None":
        if state.get("action") != Action.MARK_READ.value:
            return None
        thread_ref = state.get("incoming_ref")
        if not thread_ref:
            return None
        thread = connector.get_thread(thread_ref)
        _check_freshness_mail(thread, state.get("source_snapshot"))
        connector.mark_read(thread_ref)
        return thread_ref

    return apply


def _make_rsvp_apply_fn(
    connector: Any, *, action: Action, respond: "Callable[[Any, str], None]"
) -> "Callable[[dict], str | None]":
    """Shared RSVP apply mechanics for RSVP_ACCEPT/RSVP_TENTATIVE (build
    prompt 30, task 6.2) — mirrors
    ``draft_approve.make_calendar_action_apply_fn``'s DECLINE_INVITE shape
    exactly: a FRESH ``connector.get_event`` fetch backs the freshness
    check and the ``needsAction`` precondition (an invite already
    responded to is stale — the human approved responding to a PENDING
    invite, not silently overwriting an existing response)."""

    def apply(state: dict) -> "str | None":
        if state.get("action") != action.value:
            return None
        event_ref = state.get("incoming_ref")
        if not event_ref:
            return None
        current = connector.get_event(event_ref)
        _check_freshness_calendar_event(current, state.get("source_snapshot"))
        if current.response_status != "needsAction":
            raise SourceChangedError(
                f"event {event_ref} is no longer needsAction "
                f"(now {current.response_status!r})"
            )
        respond(connector, event_ref)
        return event_ref

    return apply


def _noop_compensate_fn(state: dict) -> None:
    """Default compensate: nothing to undo (dev/tests without a connector)."""


def make_draft_reply_compensate_fn(connector: Any) -> "Callable[[dict], None]":
    """Build the production ``compensate`` for DRAFT_REPLY (build prompt
    31, task 1): delete the created draft via ``connector.delete_draft``.
    No separate freshness check beyond the delete call itself — deleting an
    already-deleted/nonexistent draft surfaces as an honest failure the
    caller records (``orchestrator.undo.undo_effect``), the same posture
    every other apply/compensate failure in this codebase already holds."""

    def compensate(state: dict) -> None:
        draft_id = state.get("applied_ref")
        if not draft_id:
            return
        connector.delete_draft(draft_id)

    return compensate


def make_create_hold_compensate_fn(connector: Any) -> "Callable[[dict], None]":
    """Build the production ``compensate`` for CREATE_HOLD (build prompt
    31, task 1): delete the created event via ``connector.delete_event`` —
    the hold's own event id is already known (``applied_ref``)."""

    def compensate(state: dict) -> None:
        event_id = state.get("applied_ref")
        if not event_id:
            return
        connector.delete_event(event_id)

    return compensate


def make_label_compensate_fn(connector: Any) -> "Callable[[dict], None]":
    """Build the production ``compensate`` for LABEL (build prompt 31, task
    1): re-add INBOX, remove the applied label. Freshness check: the
    thread must still carry the label and lack INBOX — exactly what apply
    set it to — or the world has moved (e.g. a human already manually
    restored it) and undo refuses rather than layering a second, possibly
    wrong effect on top."""

    def compensate(state: dict) -> None:
        thread_ref = state.get("incoming_ref")
        label_name = state.get("label_name")
        if not thread_ref or not label_name:
            return
        thread = connector.get_thread(thread_ref)
        if "INBOX" in thread.labels or label_name not in thread.labels:
            raise SourceChangedError(
                f"thread {thread_ref} no longer matches the applied LABEL "
                "effect -- already changed since apply"
            )
        connector.add_label(thread_id=thread_ref, label="INBOX")
        connector.remove_label(thread_ref, label=label_name)

    return compensate


def make_decline_invite_compensate_fn(connector: Any) -> "Callable[[dict], None]":
    """Build the production ``compensate`` for DECLINE_INVITE (build
    prompt 31, task 1): reset the principal's own responseStatus to
    needsAction. Freshness check: the event must still show "declined" —
    the value apply set it to — or refuse."""

    def compensate(state: dict) -> None:
        event_ref = state.get("incoming_ref")
        if not event_ref:
            return
        current = connector.get_event(event_ref)
        if current.response_status != "declined":
            raise SourceChangedError(
                f"event {event_ref} is no longer 'declined' "
                f"(now {current.response_status!r}) -- already changed "
                "since apply"
            )
        connector.reset_invite_response(event_ref)

    return compensate


def make_reschedule_compensate_fn(connector: Any) -> "Callable[[dict], None]":
    """Build the production ``compensate`` for RESCHEDULE (build prompt
    31, task 1): restore the prior start/end captured into state BEFORE
    the patch (``dispatcher._offer_reschedule_proposal``) — never
    re-derived after the fact, since by undo time the event's start IS the
    moved-to time, not the original. Freshness check: the event must
    still be at the moved-to time apply set it to, or refuse."""

    def compensate(state: dict) -> None:
        from datetime import datetime

        event_ref = state.get("incoming_ref")
        prior_start = state.get("reschedule_prior_start")
        prior_end = state.get("reschedule_prior_end")
        moved_to_start = state.get("reschedule_start")
        if not event_ref or not prior_start or not prior_end:
            return
        current = connector.get_event(event_ref)
        if moved_to_start and current.start.isoformat() != moved_to_start:
            raise SourceChangedError(
                f"event {event_ref} start changed to "
                f"{current.start.isoformat()} since the reschedule apply "
                "-- already changed since apply"
            )
        connector.reschedule_event(
            event_ref,
            new_start=datetime.fromisoformat(prior_start),
            new_end=datetime.fromisoformat(prior_end),
        )

    return compensate


def make_accept_invite_apply_fn(connector: Any) -> "Callable[[dict], str | None]":
    """Build the production ``apply_fn`` for RSVP_ACCEPT (build prompt 30,
    task 6.2) — materializes via ``connector.accept_invite``."""
    return _make_rsvp_apply_fn(
        connector, action=Action.RSVP_ACCEPT,
        respond=lambda c, event_ref: c.accept_invite(event_ref),
    )


def make_tentative_invite_apply_fn(connector: Any) -> "Callable[[dict], str | None]":
    """Build the production ``apply_fn`` for RSVP_TENTATIVE (build prompt
    30, task 6.2) — materializes via ``connector.tentative_invite``."""
    return _make_rsvp_apply_fn(
        connector, action=Action.RSVP_TENTATIVE,
        respond=lambda c, event_ref: c.tentative_invite(event_ref),
    )


@dataclass(frozen=True)
class Capability:
    """Everything that varies per Attune action (build prompt 30, task 1).

    ``connector_probe``/``enabled_flag`` are attribute NAMES (strings), not
    bound callables/values — :func:`capability_gates_pass` resolves them
    against a live connector/settings object per call, the same pattern
    every existing hand-written gate function (``dispatcher._label_gates_
    pass``, ``_send_reply_gates_pass``) already used, just named once
    instead of once per capability. ``None`` means this capability has no
    such gate (e.g. drafting a reply needs no connector probe or opt-in
    flag beyond the matrix rung itself — matches today's behavior exactly).
    """

    action: Action
    domain: Domain
    risk_tier: RiskTier
    propose: Callable[..., str]
    apply: Callable[[dict], "str | None"]
    connector_probe: "str | None" = None
    enabled_flag: "str | None" = None
    compensate: "Callable[..., None] | None" = None
    irreversible: bool = False
    freshness_check: "Callable[..., None] | None" = None
    render_card: "Callable[[dict], str] | None" = None
    confirmation_text: "Callable[..., str] | None" = None
    rank: "Callable[..., Any] | None" = None
    max_per_run: "int | None" = None
    thread_namespace: str = ""

    def __post_init__(self) -> None:
        if self.compensate is None and not self.irreversible:
            raise ValueError(
                f"capability {self.action.value!r} declares neither a "
                "compensate action nor irreversible=True — build prompt "
                "30's own constraint: never ship a capability without one "
                "or the other."
            )


class CapabilityRegistry:
    """Immutable, exact-action-keyed registry. Duck-typed ``.get()`` is what
    ``build_draft_approve_graph``'s ``draft``/``apply`` nodes consult — see
    that function's ``registry`` parameter."""

    def __init__(self, capabilities: "tuple[Capability, ...]"):
        by_action: dict[Action, Capability] = {}
        for capability in capabilities:
            if capability.action in by_action:
                raise ValueError(
                    f"duplicate capability registration for "
                    f"{capability.action.value!r}"
                )
            by_action[capability.action] = capability
        self._by_action = by_action

    def get(self, action: "Action | str | None") -> "Capability | None":
        """Resolve by ``Action`` or its string value; ``None``/an unknown
        value returns ``None`` rather than raising — a graph node consulting
        this is on a path a human may be waiting on (approve/resume), and a
        registry miss must fall back to whatever the caller closed over,
        never crash the workflow."""
        if action is None:
            return None
        if not isinstance(action, Action):
            try:
                action = Action(action)
            except ValueError:
                return None
        return self._by_action.get(action)

    def __iter__(self):
        return iter(self._by_action.values())

    def __len__(self) -> int:
        return len(self._by_action)


def capability_gates_pass(
    capability: Capability,
    *,
    connector: Any,
    enabled: bool = True,
    matrix: Any,
    priority: "str | None" = None,
    tier: "str | None" = None,
) -> bool:
    """The three-independent-gates structure every write capability in this
    codebase already followed by hand (``dispatcher._label_gates_pass``,
    ``_send_reply_gates_pass``, ``_decline_gates_pass``,
    ``_reschedule_gates_pass``) — generalized so a NEW capability gets it
    for free instead of a fifth near-identical function: an explicit matrix
    grant at PROPOSE or above for this priority/tier context, a connector
    that structurally supports the operation, and the deployment's own
    opt-in flag. All three are independent; any one absent refuses.

    ``enabled`` is the ALREADY-RESOLVED opt-in flag value (e.g.
    ``settings.mail_labels_enabled``) — matching the existing hand-written
    gate functions' own calling convention (they take a resolved bool
    parameter, never a ``Settings`` object) rather than doing the
    attribute lookup here. Defaults to ``True`` so a capability with no
    ``enabled_flag`` (e.g. drafting) doesn't need a caller to pass one. A
    capability with no ``connector_probe``/``enabled_flag`` at all is
    gated by the matrix rung alone, matching its existing behavior.

    This build prompt (30) deliberately does NOT retrofit the four
    existing hand-written gate functions onto this one — they are
    safety-critical, heavily tested code with no acceptance-criterion
    benefit from a purely cosmetic dedup (see docs/decisions.md). This
    function is for NEW capabilities only, so they don't need a sixth
    near-identical hand-written gate function.
    """
    if capability.enabled_flag and not enabled:
        return False
    if capability.connector_probe and not getattr(
        connector, capability.connector_probe, lambda: False
    )():
        return False
    return (
        matrix.max_rung(
            capability.action, capability.domain, priority=priority, tier=tier
        )
        >= Rung.PROPOSE
    )


def build_capability_registry(
    *,
    apply_fn: "Callable[[dict], str | None] | None" = None,
    label_apply_fn: "Callable[[dict], str | None] | None" = None,
    calendar_action_apply_fn: "Callable[[dict], str | None] | None" = None,
    add_label_apply_fn: "Callable[[dict], str | None] | None" = None,
    remove_label_apply_fn: "Callable[[dict], str | None] | None" = None,
    mark_read_apply_fn: "Callable[[dict], str | None] | None" = None,
    accept_invite_apply_fn: "Callable[[dict], str | None] | None" = None,
    tentative_invite_apply_fn: "Callable[[dict], str | None] | None" = None,
    # Build prompt 31, task 1: compensating actions for the five
    # capabilities that get a real undo path. Same override-or-no-op
    # pattern as every apply_fn parameter above; production
    # (``runtime.build_runtime``) binds each ``make_*_compensate_fn
    # (connector)``. SEND_REPLY has no such parameter — it stays
    # ``irreversible=True`` unconditionally (see that Capability's own
    # registration below).
    draft_reply_compensate_fn: "Callable[[dict], None] | None" = None,
    label_compensate_fn: "Callable[[dict], None] | None" = None,
    create_hold_compensate_fn: "Callable[[dict], None] | None" = None,
    decline_invite_compensate_fn: "Callable[[dict], None] | None" = None,
    reschedule_compensate_fn: "Callable[[dict], None] | None" = None,
) -> CapabilityRegistry:
    """The production registry (build prompt 30, task 1). Mirrors
    ``app.build_app``'s existing three apply-fn parameters exactly — each
    already arrives pre-bound to a connector (``runtime.build_runtime``
    calls ``make_connector_apply_fn(connector)``/``make_label_apply_fn
    (connector)``/``make_calendar_action_apply_fn(connector)`` before ever
    calling ``build_app``), so this function only NAMES which capability
    each one backs; none of their behavior changes. A ``None`` falls back
    to the same no-op ``apply_fn`` :func:`build_draft_approve_graph` always
    defaulted to (dev/tests without a connector) — this is what keeps
    ``build_app``'s existing ``apply_fn=...`` test overrides (e.g.
    ``test_build_app_passes_apply_fn_to_graph``) reaching the DRAFT_REPLY
    capability's ``apply`` exactly as before."""
    shared_apply = apply_fn or _noop_apply_fn
    label_apply = label_apply_fn or _noop_apply_fn
    calendar_action_apply = calendar_action_apply_fn or _noop_apply_fn
    add_label_apply = add_label_apply_fn or _noop_apply_fn
    remove_label_apply = remove_label_apply_fn or _noop_apply_fn
    mark_read_apply = mark_read_apply_fn or _noop_apply_fn
    accept_invite_apply = accept_invite_apply_fn or _noop_apply_fn
    tentative_invite_apply = tentative_invite_apply_fn or _noop_apply_fn
    draft_reply_compensate = draft_reply_compensate_fn or _noop_compensate_fn
    label_compensate = label_compensate_fn or _noop_compensate_fn
    create_hold_compensate = create_hold_compensate_fn or _noop_compensate_fn
    decline_invite_compensate = decline_invite_compensate_fn or _noop_compensate_fn
    reschedule_compensate = reschedule_compensate_fn or _noop_compensate_fn

    return CapabilityRegistry((
        Capability(
            action=Action.DRAFT_REPLY,
            domain=Domain.MAIL,
            risk_tier=RiskTier.R2,
            propose=_default_propose_fn,
            apply=shared_apply,
            freshness_check=_check_freshness_mail,
            confirmation_text=apply_confirmation,
            # Build prompt 31, task 1: the compensating action for a
            # created-but-never-sent draft is deleting it — see
            # make_draft_reply_compensate_fn. This is what makes DRAFT_REPLY
            # honestly reversible (contrast SEND_REPLY below, which stays
            # irreversible=True unconditionally).
            compensate=draft_reply_compensate,
            thread_namespace="gmail:",
        ),
        Capability(
            action=Action.SEND_REPLY,
            domain=Domain.MAIL,
            risk_tier=RiskTier.R3,
            propose=_default_propose_fn,
            # Shares the exact same apply callable as DRAFT_REPLY:
            # make_connector_apply_fn already branches on
            # state["action"] == SEND_REPLY internally (create the draft,
            # then send it) — see that function's own docstring.
            apply=shared_apply,
            connector_probe="supports_sending",
            enabled_flag="mail_send_enabled",
            freshness_check=_check_freshness_mail,
            confirmation_text=apply_confirmation,
            # Build prompt 31: SEND_REPLY stays irreversible=True
            # UNCONDITIONALLY — a sent reply is irreversible by
            # construction, and a follow-up "please ignore that" email is
            # not an undo. Do not fake it with a compensate function (see
            # docs/decisions.md).
            irreversible=True,
            thread_namespace="gmail:",
        ),
        Capability(
            action=Action.LABEL,
            domain=Domain.MAIL,
            risk_tier=RiskTier.R2,
            propose=archive_draft_fn,
            apply=label_apply,
            connector_probe="supports_labeling",
            enabled_flag="mail_labels_enabled",
            freshness_check=_check_freshness_mail,
            confirmation_text=apply_confirmation,
            # Build prompt 31, task 1: re-add INBOX, remove the applied
            # label — see make_label_compensate_fn.
            compensate=label_compensate,
            render_card=lambda state: (
                f"Archive proposal — triaged noise: {state.get('subject')}"
            ),
            thread_namespace="archive:",
        ),
        Capability(
            action=Action.CREATE_HOLD,
            domain=Domain.CALENDAR,
            risk_tier=RiskTier.R2,
            propose=_default_propose_fn,
            # Shares the exact same apply callable as DRAFT_REPLY/
            # SEND_REPLY: make_connector_apply_fn branches on
            # state["domain"] == "calendar" internally.
            apply=shared_apply,
            confirmation_text=apply_confirmation,
            # Build prompt 31, task 1: delete the created event — see
            # make_create_hold_compensate_fn. The hold's own event id is
            # already known (applied_ref).
            compensate=create_hold_compensate,
            thread_namespace="calendar:",
        ),
        Capability(
            action=Action.DECLINE_INVITE,
            domain=Domain.CALENDAR,
            risk_tier=RiskTier.R3,
            propose=calendar_action_draft_fn,
            apply=calendar_action_apply,
            connector_probe="supports_calendar_writes",
            enabled_flag="calendar_writes_enabled",
            freshness_check=_check_freshness_calendar_event,
            confirmation_text=apply_confirmation,
            # Build prompt 31, task 1: reset the principal's own
            # responseStatus to needsAction — see
            # make_decline_invite_compensate_fn.
            compensate=decline_invite_compensate,
            render_card=lambda state: f"Decline invite proposal: {state.get('subject')}",
            thread_namespace="decline:",
        ),
        Capability(
            action=Action.RESCHEDULE,
            domain=Domain.CALENDAR,
            risk_tier=RiskTier.R3,
            propose=calendar_action_draft_fn,
            apply=calendar_action_apply,
            connector_probe="supports_calendar_writes",
            enabled_flag="calendar_writes_enabled",
            freshness_check=_check_freshness_calendar_event,
            confirmation_text=apply_confirmation,
            # Build prompt 31, task 1: restore the prior start/end,
            # captured into state BEFORE the patch (dispatcher.
            # _offer_reschedule_proposal) — see
            # make_reschedule_compensate_fn.
            compensate=reschedule_compensate,
            thread_namespace="calendar:reschedule:",
        ),
        Capability(
            action=Action.FOLLOW_UP,
            domain=Domain.MAIL,
            risk_tier=RiskTier.R2,
            propose=_default_propose_fn,
            # Shares DRAFT_REPLY's apply callable — a follow-up nudge IS a
            # mail draft (domain="mail"); make_connector_apply_fn doesn't
            # distinguish it from any other mail draft at apply time.
            apply=shared_apply,
            freshness_check=_check_freshness_mail,
            confirmation_text=apply_confirmation,
            irreversible=True,
            thread_namespace="followup:",
        ),
        # Build prompt 30, task 6.1: generic label/mark-read hygiene —
        # distinct from LABEL (specifically "apply the noise label AND
        # archive"). No default matrix grant (see autonomy.default_matrix):
        # new capabilities are not granted by arriving.
        Capability(
            action=Action.ADD_LABEL,
            domain=Domain.MAIL,
            risk_tier=RiskTier.R1,
            propose=archive_draft_fn,
            apply=add_label_apply,
            connector_probe="supports_add_label",
            enabled_flag="mail_labels_enabled",
            freshness_check=_check_freshness_mail,
            confirmation_text=apply_confirmation,
            irreversible=True,
            render_card=lambda state: f"Add label proposal: {state.get('label_name')} on {state.get('subject')}",
            thread_namespace="add_label:",
        ),
        Capability(
            action=Action.REMOVE_LABEL,
            domain=Domain.MAIL,
            risk_tier=RiskTier.R2,
            propose=archive_draft_fn,
            apply=remove_label_apply,
            connector_probe="supports_labeling",
            enabled_flag="mail_labels_enabled",
            freshness_check=_check_freshness_mail,
            confirmation_text=apply_confirmation,
            irreversible=True,
            render_card=lambda state: f"Remove label proposal: {state.get('label_name')} on {state.get('subject')}",
            thread_namespace="remove_label:",
        ),
        Capability(
            action=Action.MARK_READ,
            domain=Domain.MAIL,
            risk_tier=RiskTier.R1,
            propose=archive_draft_fn,
            apply=mark_read_apply,
            connector_probe="supports_labeling",
            enabled_flag="mail_labels_enabled",
            freshness_check=_check_freshness_mail,
            confirmation_text=apply_confirmation,
            irreversible=True,
            render_card=lambda state: f"Mark read proposal: {state.get('subject')}",
            thread_namespace="mark_read:",
        ),
        # Build prompt 30, task 6.2: the positive RSVP counterparts to
        # DECLINE_INVITE. Same posture: no default matrix grant.
        Capability(
            action=Action.RSVP_ACCEPT,
            domain=Domain.CALENDAR,
            risk_tier=RiskTier.R2,
            propose=calendar_action_draft_fn,
            apply=accept_invite_apply,
            connector_probe="supports_calendar_writes",
            enabled_flag="calendar_writes_enabled",
            freshness_check=_check_freshness_calendar_event,
            confirmation_text=apply_confirmation,
            irreversible=True,
            render_card=lambda state: f"Accept invite proposal: {state.get('subject')}",
            thread_namespace="rsvp_accept:",
        ),
        Capability(
            action=Action.RSVP_TENTATIVE,
            domain=Domain.CALENDAR,
            risk_tier=RiskTier.R2,
            propose=calendar_action_draft_fn,
            apply=tentative_invite_apply,
            connector_probe="supports_calendar_writes",
            enabled_flag="calendar_writes_enabled",
            freshness_check=_check_freshness_calendar_event,
            confirmation_text=apply_confirmation,
            irreversible=True,
            render_card=lambda state: f"Tentative RSVP proposal: {state.get('subject')}",
            thread_namespace="rsvp_tentative:",
        ),
    ))
