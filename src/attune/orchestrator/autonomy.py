"""The autonomy ladder and per-action/per-domain permission matrix (design 3.2).

This is the single most important safety primitive in the project, and the
direct architectural answer to the OpenClaw failure mode surveyed in the design
doc (8.1): never grant blanket autonomy over a privileged agent exposed to
untrusted input. Autonomy is *earned per (action, domain)*, never global.

Nothing here executes anything. It's the gate other modules consult before
acting: "am I allowed to *send* a reply on the *personal-mail* domain right now,
or only *draft* it?" The orchestrator asks; this answers.

Scoped grants (Phase 4 stage 1, G14) — signal-scoped autonomy
---------------------------------------------------------------

A (action, domain) pair can now hold MULTIPLE grants, each optionally scoped
to a :class:`GrantScope` predicate over the two signals the orchestrator
already computes: triage ``priority`` ("urgent"/"routine"/"noise") and the
sender's importance ``tier`` ("high"/"normal"/"low"). This is what makes
"auto-file NOISE newsletters" and "auto-draft ROUTINE replies from known
senders, always interrupt for URGENT" expressible as grants, rather than
requiring a new gate for every such rule.

Two rules make this fail-closed rather than merely convenient:

1. **Missing context never matches a predicate.** A grant scoped to
   ``priorities={"routine"}`` does not apply to an item whose priority is
   unknown (e.g. no importance profile, no sender) — it falls back to
   whatever unscoped grant exists, or the READ_ONLY floor. A predicate that
   is ``None`` (unset) matches anything, including missing context — this is
   what makes an ordinary unscoped grant behave exactly as it always has.

2. **The URGENT interrupt rule.** Regardless of scope matching, when the
   evaluation context's priority is "urgent", any matched rung ABOVE PROPOSE
   is capped down to PROPOSE — UNLESS that grant's own scope explicitly lists
   "urgent" in its ``priorities`` set. Practically: an unscoped ACT_NOTIFY
   grant on (DRAFT_REPLY, MAIL) will auto-apply a ROUTINE reply but still
   interrupt for an URGENT one, because "always interrupt for URGENT" is the
   product's default posture. Auto-acting on urgent items is not the default
   for any grant — it requires a human to deliberately write "urgent" into
   that grant's scope, an explicit, reviewable choice rather than something
   that falls out of granting a rung.

Grant/revoke stay CLI-only and additive/subtractive per (action, domain,
scope) triple — see ``grants.py`` and ``cli/autonomy_cmd.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, Enum
from typing import Any


class Rung(IntEnum):
    """The earned-autonomy ladder. Higher = more autonomy."""

    READ_ONLY = 1          # observe and summarize; take no action
    PROPOSE = 2            # draft the action; wait for explicit approval
    ACT_NOTIFY = 3         # execute low-risk reversible action, notify after
    AUTONOMOUS = 4         # no notification needed (rare, explicitly graduated)


class Domain(str, Enum):
    """Data domains permissions are scoped to."""

    MAIL = "mail"
    CALENDAR = "calendar"
    CHAT = "chat"
    SLACK = "slack"


class Action(str, Enum):
    """Action types permissions are scoped to. Note draft vs send are distinct
    permissions, as are 'act on holds with no external attendees' vs 'anything'."""

    SUMMARIZE = "summarize"
    DRAFT_REPLY = "draft_reply"
    SEND_REPLY = "send_reply"
    CREATE_HOLD = "create_hold"
    DECLINE_INVITE = "decline_invite"
    RESCHEDULE = "reschedule"
    LABEL = "label"
    FOLLOW_UP = "follow_up"


class _Unset:
    """Sentinel distinguishing "no scope argument given" from an explicit
    ``scope=None`` (which targets the unscoped grant specifically) —
    :meth:`PermissionMatrix.revoke`'s default has to tell those apart:
    omitting the argument entirely revokes every scope for the pair (the
    pre-scoping behavior), while ``scope=None`` revokes only the unscoped
    entry, leaving any scoped grants for that pair untouched."""

    def __repr__(self) -> str:  # pragma: no cover — debugging aid only
        return "UNSET"


UNSET: Any = _Unset()


@dataclass(frozen=True)
class GrantScope:
    """An optional predicate narrowing a grant to a context.

    ``priorities``/``tiers`` are ``None`` (no predicate — matches any value,
    including missing context) or a non-empty ``frozenset`` of the
    corresponding enum's string values (``triage.Priority`` /
    ``importance.ImportanceTier``). Kept as plain strings here (not the enums
    themselves) so this module doesn't need to import ``triage``/
    ``importance`` — validation against the real enums happens where scopes
    are constructed from user input (``grants.parse_scope``).

    A scope with both fields ``None`` is the unscoped grant; callers should
    canonicalize that to bare ``scope=None`` rather than constructing one
    (``PermissionMatrix.grant``/``revoke`` do this for you).
    """

    priorities: "frozenset[str] | None" = None
    tiers: "frozenset[str] | None" = None

    def matches(self, priority: "str | None", tier: "str | None") -> bool:
        """Fail-closed predicate matching: a predicate with values matches
        only when the corresponding context value is PRESENT and a member of
        the set. Missing context (``None``) never satisfies a predicate that
        has values — a priority-scoped grant cannot apply to an item whose
        priority is unknown. A ``None`` predicate (no values) matches
        anything, including missing context."""
        if self.priorities is not None:
            if priority is None or priority not in self.priorities:
                return False
        if self.tiers is not None:
            if tier is None or tier not in self.tiers:
                return False
        return True

    def is_unscoped(self) -> bool:
        return self.priorities is None and self.tiers is None


def _canonical_scope(scope: "GrantScope | None") -> "GrantScope | None":
    """Normalize a scope with no predicates at all to bare ``None`` — the
    unscoped grant has exactly one representation in storage."""
    if scope is not None and scope.is_unscoped():
        return None
    return scope


@dataclass(frozen=True)
class ScopedGrant:
    """One grant entry: a rung, optionally narrowed to a :class:`GrantScope`.
    ``scope=None`` is the unscoped grant — matches any context."""

    rung: Rung
    scope: "GrantScope | None" = None


@dataclass(frozen=True)
class PermissionMatrix:
    """Maps (Action, Domain) -> the grants currently held for that pair.

    Anything not present defaults to READ_ONLY: the safe floor. Grants are
    added deliberately as trust is built, and can be clawed back by removing
    them. A pair may hold MULTIPLE grants (Phase 4 stage 1) — an unscoped
    grant plus any number of scoped ones — and the effective rung for a
    given evaluation context is the max over every grant whose scope matches
    that context (see :meth:`max_rung`).
    """

    grants: "dict[tuple[Action, Domain], tuple[ScopedGrant, ...]]" = field(
        default_factory=dict
    )

    def max_rung(
        self,
        action: Action,
        domain: Domain,
        *,
        priority: "str | None" = None,
        tier: "str | None" = None,
    ) -> Rung:
        """The highest rung any matching grant confers for this context.

        Called with no ``priority``/``tier`` (every pre-Phase-4 call site),
        only unscoped grants can match — fail-closed matching means a scoped
        predicate never matches missing context — so this is byte-identical
        to the pre-scoping behavior. The URGENT interrupt rule (module
        docstring) is applied per matching grant before taking the max.
        """
        best = Rung.READ_ONLY
        for sg in self.grants.get((action, domain), ()):
            if sg.scope is not None and not sg.scope.matches(priority, tier):
                continue
            rung = sg.rung
            if priority == "urgent" and rung > Rung.PROPOSE:
                urgent_scoped = (
                    sg.scope is not None
                    and sg.scope.priorities is not None
                    and "urgent" in sg.scope.priorities
                )
                if not urgent_scoped:
                    rung = Rung.PROPOSE
            if rung > best:
                best = rung
        return best

    def allows(
        self,
        action: Action,
        domain: Domain,
        rung: Rung,
        *,
        priority: "str | None" = None,
        tier: "str | None" = None,
    ) -> bool:
        """True if performing ``action`` on ``domain`` at ``rung`` is
        permitted in this context."""
        return rung <= self.max_rung(action, domain, priority=priority, tier=tier)

    def grant(
        self,
        action: Action,
        domain: Domain,
        rung: Rung,
        *,
        scope: "GrantScope | None" = None,
    ) -> "PermissionMatrix":
        """Return a new matrix with a grant added/replaced (immutable
        update). Replaces an existing grant for this (action, domain) that
        has the SAME scope; otherwise appends a new one alongside whatever
        grants already exist for the pair."""
        norm_scope = _canonical_scope(scope)
        new = dict(self.grants)
        existing = list(new.get((action, domain), ()))
        for i, sg in enumerate(existing):
            if sg.scope == norm_scope:
                existing[i] = ScopedGrant(rung, norm_scope)
                break
        else:
            existing.append(ScopedGrant(rung, norm_scope))
        new[(action, domain)] = tuple(existing)
        return PermissionMatrix(new)

    def revoke(
        self,
        action: Action,
        domain: Domain,
        *,
        scope: "GrantScope | None | _Unset" = UNSET,
    ) -> "PermissionMatrix":
        """Return a new matrix without this grant (immutable update, like
        ``grant``). No ``scope`` argument revokes EVERY grant for the pair
        (the pre-scoping behavior — falls all the way to the READ_ONLY
        floor). An explicit ``scope`` (including ``scope=None`` for the
        unscoped grant specifically) revokes only the matching-scope entry,
        leaving any other scoped grants for the pair untouched."""
        new = dict(self.grants)
        if scope is UNSET:
            new.pop((action, domain), None)
        else:
            norm_scope = _canonical_scope(scope)
            remaining = tuple(
                sg for sg in new.get((action, domain), ()) if sg.scope != norm_scope
            )
            if remaining:
                new[(action, domain)] = remaining
            else:
                new.pop((action, domain), None)
        return PermissionMatrix(new)


def default_matrix() -> PermissionMatrix:
    """The starting posture: everything read-only, drafting allowed everywhere,
    nothing sent or acted on autonomously. Matches 'expect almost everything to
    live at rung 1-2 for months' in the design doc."""
    m = PermissionMatrix()
    for domain in Domain:
        m = m.grant(Action.SUMMARIZE, domain, Rung.READ_ONLY)
    m = m.grant(Action.DRAFT_REPLY, Domain.MAIL, Rung.PROPOSE)
    m = m.grant(Action.DRAFT_REPLY, Domain.CHAT, Rung.PROPOSE)
    m = m.grant(Action.DRAFT_REPLY, Domain.SLACK, Rung.PROPOSE)
    m = m.grant(Action.CREATE_HOLD, Domain.CALENDAR, Rung.PROPOSE)
    # Follow-up nudges are their own action type (not DRAFT_REPLY) — honest
    # to the matrix's action-type granularity: "may propose follow-ups" and
    # "may propose replies" are separately grantable/revocable.
    m = m.grant(Action.FOLLOW_UP, Domain.MAIL, Rung.PROPOSE)
    # LABEL (Phase 3 stage 1, G9 — the first hygiene write: archiving mail
    # already triaged NOISE) is granted PROPOSE by default for the same
    # reason DRAFT_REPLY is: proposing is safe. The effect (archiving) still
    # requires a human to approve the card; this grant only lets the
    # proposal exist, and it's one of three independent gates the dispatcher
    # checks before ever building one — a connector must also structurally
    # support labeling (``supports_labeling()``) and the deployment must have
    # opted in (``ATTUNE_MAIL_LABELS_ENABLED``). See docs/decisions.md.
    m = m.grant(Action.LABEL, Domain.MAIL, Rung.PROPOSE)
    # DECLINE_INVITE/RESCHEDULE (Phase 3 stage 2 -- hygiene/logistics writes
    # on the calendar domain) get the same PROPOSE-by-default posture as
    # LABEL, for the same reason: proposing is safe, since the effect still
    # requires a human's approval. Each is one of three independent gates
    # the dispatcher checks before ever building a proposal -- a connector
    # must also structurally support calendar writes
    # (``supports_calendar_writes()``) and the deployment must have opted in
    # (``ATTUNE_CALENDAR_WRITES_ENABLED``). See docs/decisions.md.
    m = m.grant(Action.DECLINE_INVITE, Domain.CALENDAR, Rung.PROPOSE)
    m = m.grant(Action.RESCHEDULE, Domain.CALENDAR, Rung.PROPOSE)
    return m
