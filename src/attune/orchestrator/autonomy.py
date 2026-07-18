"""The autonomy ladder and per-action/per-domain permission matrix (design 3.2).

This is the single most important safety primitive in the project, and the
direct architectural answer to the OpenClaw failure mode surveyed in the design
doc (8.1): never grant blanket autonomy over a privileged agent exposed to
untrusted input. Autonomy is *earned per (action, domain)*, never global.

Nothing here executes anything. It's the gate other modules consult before
acting: "am I allowed to *send* a reply on the *personal-mail* domain right now,
or only *draft* it?" The orchestrator asks; this answers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, Enum


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


@dataclass(frozen=True)
class PermissionMatrix:
    """Maps (Action, Domain) -> the max Rung currently granted.

    Anything not present defaults to READ_ONLY: the safe floor. Grants are added
    deliberately as trust is built, and can be clawed back by removing them.
    """

    grants: dict[tuple[Action, Domain], Rung] = field(default_factory=dict)

    def max_rung(self, action: Action, domain: Domain) -> Rung:
        return self.grants.get((action, domain), Rung.READ_ONLY)

    def allows(self, action: Action, domain: Domain, rung: Rung) -> bool:
        """True if performing ``action`` on ``domain`` at ``rung`` is permitted."""
        return rung <= self.max_rung(action, domain)

    def grant(self, action: Action, domain: Domain, rung: Rung) -> "PermissionMatrix":
        """Return a new matrix with an added/updated grant (immutable update)."""
        new = dict(self.grants)
        new[(action, domain)] = rung
        return PermissionMatrix(new)

    def revoke(self, action: Action, domain: Domain) -> "PermissionMatrix":
        """Return a new matrix without this grant — the (action, domain)
        falls back to the READ_ONLY floor (immutable update, like grant)."""
        new = dict(self.grants)
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
