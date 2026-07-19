"""Autonomy grants: persistence, operations, and the earning mechanism
(design 3.2, roadmap prompt 12).

"Autonomy is earned, not granted" is the design's second pillar, but until
this module the earning mechanism didn't exist: `default_matrix()` was
hardcoded, grants survived neither a restart nor existed anywhere a user
could make one, and nothing computed the track record the whole ladder
concept depends on. The audit log already records every ``autonomy_gate``
and ``human_decision`` — this module folds those into per-(action, domain)
records and *suggestions*.

Safety spine — no shortcuts (rule 3):

- **A human always makes the grant.** No code path here (or anywhere) may
  auto-apply a suggestion, however strong the record. Suggestions are
  information only.
- **Grant/revoke is CLI-only.** The chat surface shows the posture and
  suggestions but cannot change them — a chat channel that relays untrusted
  content must not be able to escalate autonomy via a spoofed-looking
  message.
- **The gate's fail-safe default is untouched**: absent a grant, everything
  routes through human approval. Granting ``SEND_REPLY`` at any rung is
  additionally *not sufficient* to send — rule 4's structural gate
  (``send_enabled`` + a real ``gmail.send`` scope) still refuses; callers
  surface that warning.
- Parsing is strict enum validation — a typo errors, never silently
  defaults. ``parse_scope`` (Phase 4 stage 1, G14) is the same discipline
  applied to the optional priority/tier scope a grant can be narrowed to —
  see ``autonomy.GrantScope`` for the matching semantics (fail-closed on
  missing context) and the urgent-interrupt rule it structurally enforces.
- ``suggest_graduations``/``track_records`` operate on unscoped grants only:
  they call ``matrix.max_rung(action, domain)`` with no priority/tier
  context, and fail-closed scope matching means a scoped grant never
  matches missing context — so a scoped grant simply doesn't participate in
  today's graduation math. Scoped suggestion generation is future work.

Phase 4 stage 2 (G13/G15) — cards, a hard ceiling, and demotion
------------------------------------------------------------------

``suggest_graduations``' output now reaches the approval channel as a real
approval card, not just digest text (``runtime.post_autonomy_digest`` /
``Runtime._post_autonomy_card``), resolved by :func:`resolve_autonomy_card`
rather than a LangGraph resume — there is no workflow behind these cards.
Two module-level constants are the HARD CEILING on what a card may ever
grant, enforced BOTH where a card is built (``runtime.py``, skip building
it) AND again here, in :func:`resolve_autonomy_card`, against the
persisted card snapshot (defense in depth: a forged or stale thread_id
must still refuse):

- ``GRADUATION_CARD_EXCLUDED_ACTIONS`` — ``SEND_REPLY`` is CLI-only,
  always. History/memory (a track record, however clean) cannot unlock
  autonomous external sends — see docs/decisions.md's "Security
  architecture is normative" entry.
- ``GRADUATION_CARD_MAX_RUNG`` — a card never offers above ``ACT_NOTIFY``.

Demotion (:func:`suggest_demotions`) is graduation's mirror image: a
rejection streak (or a single rejection of an auto-applied effect —
stronger evidence) suggests dropping a granted (action, domain) back to
``PROPOSE`` — always all the way back to human-approval-per-item, never a
gradual one-rung step, and never auto-applied.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..fslock import locked
from .autonomy import (
    UNSET,
    Action,
    Domain,
    GrantScope,
    PermissionMatrix,
    Rung,
    ScopedGrant,
)
from .importance import ImportanceTier
from .triage import Priority

GRADUATION_MIN_DECISIONS = 10
GRADUATION_MIN_APPROVAL_RATE = 0.95

# Phase 4 stage 2 (G13) — the hard ceiling on what an approval CARD may
# ever grant (CLI grants are unaffected: a human can still explicitly grant
# SEND_REPLY or AUTONOMOUS via `attune autonomy grant`). See the module
# docstring section above.
GRADUATION_CARD_EXCLUDED_ACTIONS = frozenset({Action.SEND_REPLY})
GRADUATION_CARD_MAX_RUNG = Rung.ACT_NOTIFY

# A rejected suggestion is suppressed from re-appearing for this many days.
GRADUATION_REJECTION_COOLDOWN_DAYS = 30

# Approval cards posted per digest run, per suggestion kind (graduation,
# demotion) — a proactive feature that spams is worse than none (design
# 8.1's Lindy critique), same posture as every other per-run cap in this
# codebase (MAX_LABEL_PROPOSALS_PER_RUN, MAX_NUDGES_PER_RUN, ...).
MAX_AUTONOMY_CARDS_PER_RUN = 3

# Thread-id namespaces for graduation/demotion cards (Phase 4 stage 2,
# G13). Distinct from every LangGraph proposal namespace ("gmail:",
# "archive:", "decline:", "calendar:reschedule:") since there is no
# workflow behind these cards at all — ``runtime._bound_resume`` routes by
# this prefix to :func:`resolve_autonomy_card` instead of a graph resume.
GRADUATION_PREFIX = "graduation:"
DEMOTION_PREFIX = "demotion:"

# Recent-decisions window for demotion (Deliverable C) — a COUNT window,
# not a calendar-time window like track_records' 30 days: a demotion signal
# should react to the most recent handful of decisions regardless of how
# long they took to accumulate.
DEMOTION_WINDOW_DECISIONS = 10
DEMOTION_MIN_REJECTIONS = 2


class JsonPermissionMatrixStore:
    """Persists grants as ``{"<action>|<domain>": [{"rung": int, "scope":
    {...}|null}, ...]}`` — one list entry per grant held for that pair
    (Phase 4 stage 1: a pair can hold more than one, each optionally scoped).

    Backward compatible with the pre-scoping schema, where the value was a
    bare rung int (always the unscoped grant): :meth:`load` accepts either
    shape, but :meth:`save` always writes the current (list) schema — so a
    file only ever migrates forward, never bounces back to the old shape.

    The file is only ever written through :func:`grant`/:func:`revoke` —
    the matrix object itself stays frozen/immutable, and ``build_app`` only
    *loads* from here.
    """

    def __init__(self, path: str):
        self._path = path

    def load(self) -> PermissionMatrix | None:
        """The persisted matrix, or None if never saved (caller falls back
        to ``default_matrix()``). Strict parsing: an unknown action/domain/
        rung in the file is a hard error, not a silent skip — a corrupted
        autonomy file must not quietly change the safety posture."""
        if not os.path.exists(self._path):
            return None
        with open(self._path) as fh:
            raw = json.load(fh)
        grants: dict[tuple[Action, Domain], tuple[ScopedGrant, ...]] = {}
        for key, value in raw.items():
            action_str, domain_str = key.split("|", 1)
            pair = (Action(action_str), Domain(domain_str))
            if isinstance(value, int):
                # Pre-scoping schema: a bare rung int is the unscoped grant.
                grants[pair] = (ScopedGrant(Rung(value), None),)
                continue
            entries = []
            for item in value:
                rung = Rung(int(item["rung"]))
                entries.append(ScopedGrant(rung, _scope_from_json(item.get("scope"))))
            grants[pair] = tuple(entries)
        return PermissionMatrix(grants)

    def save(self, matrix: PermissionMatrix) -> None:
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        data: dict[str, Any] = {
            f"{action.value}|{domain.value}": [
                {"rung": int(sg.rung), "scope": _scope_to_json(sg.scope)}
                for sg in entries
            ]
            for (action, domain), entries in matrix.grants.items()
        }
        directory = parent or "."
        fd, temp_path = tempfile.mkstemp(prefix=".autonomy-", dir=directory)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp_path, self._path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)


def _scope_to_json(scope: GrantScope | None) -> dict[str, Any] | None:
    if scope is None:
        return None
    return {
        "priorities": sorted(scope.priorities) if scope.priorities is not None else None,
        "tiers": sorted(scope.tiers) if scope.tiers is not None else None,
    }


def _scope_from_json(raw: dict[str, Any] | None) -> GrantScope | None:
    if not raw:
        return None
    priorities = raw.get("priorities")
    tiers = raw.get("tiers")
    if priorities is None and tiers is None:
        return None
    return GrantScope(
        priorities=frozenset(priorities) if priorities is not None else None,
        tiers=frozenset(tiers) if tiers is not None else None,
    )


def make_matrix_provider(
    store: JsonPermissionMatrixStore,
) -> Any:
    """A live matrix source for the gate (review finding #2): stat the file
    per evaluation, reload only on mtime change. Grants and — critically —
    REVOCATIONS take effect on the next gate evaluation, not the next
    restart.

    Failure posture: an unreadable, corrupt, or deleted file fails closed to
    ``default_matrix()`` and logs the failure. Atomic store writes make a
    transient half-written file unnecessary for normal grant/revoke updates.
    """
    import logging

    from .autonomy import default_matrix

    logger = logging.getLogger(__name__)
    cache: dict[str, Any] = {"mtime": None, "matrix": None}

    def provider() -> PermissionMatrix:
        try:
            mtime = os.path.getmtime(store._path)
        except OSError:
            mtime = None  # never saved -> conservative default

        if mtime is None:
            cache["mtime"] = None
            cache["matrix"] = default_matrix()
            return cache["matrix"]

        if mtime != cache["mtime"]:
            try:
                loaded = store.load()
            except (ValueError, KeyError, OSError) as exc:
                logger.warning(
                    "autonomy grants file unreadable (%s: %s) — keeping the "
                    "conservative default matrix", type(exc).__name__, exc,
                )
                cache["mtime"] = None
                cache["matrix"] = default_matrix()
                return cache["matrix"]
            cache["mtime"] = mtime
            cache["matrix"] = loaded if loaded is not None else default_matrix()
        return cache["matrix"]

    return provider


def grant(
    store: JsonPermissionMatrixStore,
    matrix: PermissionMatrix,
    action: Action,
    domain: Domain,
    rung: Rung,
    *,
    scope: GrantScope | None = None,
    audit_log: Any,
    user_id: str,
) -> PermissionMatrix:
    """Apply and persist one grant — the most audit-worthy event in the
    system. ``scope`` narrows the grant to a priority/tier predicate
    (Phase 4 stage 1); omitted, it's the unscoped grant, matching any
    context. Returns the new matrix (the old one is unchanged)."""
    new_matrix = matrix.grant(action, domain, rung, scope=scope)
    store.save(new_matrix)
    _audit_grant_event(
        audit_log, "autonomy_granted", action, domain, user_id,
        rung=int(rung), **_scope_audit_fields(scope),
    )
    return new_matrix


def revoke(
    store: JsonPermissionMatrixStore,
    matrix: PermissionMatrix,
    action: Action,
    domain: Domain,
    *,
    scope: Any = UNSET,
    audit_log: Any,
    user_id: str,
) -> PermissionMatrix:
    """Claw back a grant and persist. No ``scope`` revokes every grant held
    for the pair (falls all the way to the READ_ONLY floor — the
    pre-scoping behavior); an explicit ``scope`` (including ``scope=None``
    for the unscoped grant specifically) revokes only that one entry."""
    new_matrix = matrix.revoke(action, domain, scope=scope)
    store.save(new_matrix)
    scope_fields = (
        {"priorities": None, "tiers": None}
        if scope is UNSET
        else _scope_audit_fields(scope)
    )
    _audit_grant_event(
        audit_log, "autonomy_revoked", action, domain, user_id, **scope_fields
    )
    return new_matrix


def _scope_audit_fields(scope: GrantScope | None) -> dict[str, Any]:
    """Content-free scope fields for a grant/revoke audit event — categorical
    values only (priority/tier names), never free text."""
    if scope is None:
        return {"priorities": None, "tiers": None}
    return {
        "priorities": sorted(scope.priorities) if scope.priorities is not None else None,
        "tiers": sorted(scope.tiers) if scope.tiers is not None else None,
    }


def render_scope(scope: GrantScope | None) -> str:
    """Readable scope suffix for a grant row, e.g.
    ``[routine; tier: high,normal]``. Empty string for the unscoped grant."""
    if scope is None:
        return ""
    parts = []
    if scope.priorities is not None:
        parts.append(",".join(sorted(scope.priorities)))
    if scope.tiers is not None:
        parts.append("tier: " + ",".join(sorted(scope.tiers)))
    return "[" + "; ".join(parts) + "]"


def show_matrix(matrix: PermissionMatrix) -> str:
    """Human-readable posture table, most-permissive first. Each grant is its
    own row (a pair may hold several — Phase 4 stage 1), with a readable
    scope suffix when the grant is narrowed to a priority/tier predicate. An
    unscoped grant at ACT_NOTIFY or above earns a footnote: the
    urgent-interrupt rule (autonomy.py) still caps it to PROPOSE for URGENT
    items unless the grant explicitly scopes "urgent" in."""
    if not matrix.grants:
        return "No grants — everything at the READ_ONLY floor."
    rows = [
        (action, domain, sg)
        for (action, domain), entries in matrix.grants.items()
        for sg in entries
    ]
    rows.sort(key=lambda row: (-int(row[2].rung), row[0].value, row[1].value))
    lines = [f"{'action':<15} {'domain':<10} rung"]
    any_unscoped_act_level = False
    for action, domain, sg in rows:
        scope_text = f" {render_scope(sg.scope)}" if sg.scope is not None else ""
        lines.append(
            f"{action.value:<15} {domain.value:<10} {int(sg.rung)} "
            f"({sg.rung.name}){scope_text}"
        )
        if sg.scope is None and sg.rung >= Rung.ACT_NOTIFY:
            any_unscoped_act_level = True
    if any_unscoped_act_level:
        lines.append("")
        lines.append(
            "Note: an unscoped grant at ACT_NOTIFY or above still interrupts "
            "for URGENT priority — the urgent-interrupt rule caps it to "
            "PROPOSE unless the grant's scope explicitly lists 'urgent'."
        )
    return "\n".join(lines)


@dataclass
class TrackRecord:
    """What the human actually did with this (action, domain)'s proposals."""

    action: Action
    domain: Domain
    approved: int = 0   # approved unedited
    edited: int = 0
    rejected: int = 0
    ignored: int = 0
    applied: int = 0
    apply_failed: int = 0

    @property
    def total(self) -> int:
        return self.approved + self.edited + self.rejected + self.ignored

    def render(self) -> str:
        return (
            f"{self.action.value} on {self.domain.value}: "
            f"{self.approved} approved unedited, {self.edited} edited, "
            f"{self.rejected} rejected, {self.ignored} ignored "
            f"({self.total} total); {self.applied} applied, "
            f"{self.apply_failed} apply failures"
        )


@dataclass
class GraduationSuggestion:
    record: TrackRecord
    to_rung: Rung = Rung.ACT_NOTIFY

    def render(self) -> str:
        r = self.record
        return (
            f"{r.approved}/{r.total} {r.action.value} proposals on "
            f"{r.domain.value} approved unedited — consider graduating to "
            f"{self.to_rung.name}: attune autonomy grant "
            f"{r.action.value} {r.domain.value} {self.to_rung.name.lower()}"
        )


def track_records(
    audit_log: Any,
    *,
    window_days: int = 30,
    now: datetime | None = None,
) -> dict[tuple[Action, Domain], TrackRecord]:
    """Fold the audit log into per-(action, domain) decision counts.

    Attribution runs by workflow thread_id: the ``autonomy_gate`` event
    carries the (action, domain); ``human_decision`` carries what the human
    did; ``approval_ignored`` (the pending-sweep) marks ignores. Successful
    and failed/skipped apply events establish whether accepted proposals
    actually reached the external system.
    ``auto_applied`` decisions are excluded — a track record measures human
    judgment on proposals, and autonomous runs aren't proposals.
    """
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=window_days)
    entries = audit_log.query(since=since)

    scope: dict[str, tuple[Action, Domain]] = {}
    outcome: dict[str, str] = {}
    execution: dict[str, str] = {}
    for entry in entries:
        if entry.event == "autonomy_gate":
            # The event's own "domain" field is absorbed into the entry-level
            # join key by AuditEntry.from_json — read it from either place.
            try:
                scope[entry.thread_id] = (
                    Action(entry.fields.get("action")),
                    Domain(entry.fields.get("domain") or entry.domain),
                )
            except ValueError:
                continue
        elif entry.event == "human_decision":
            outcome[entry.thread_id] = entry.fields.get("decision", "")
        elif entry.event == "approval_ignored":
            outcome.setdefault(entry.thread_id, "ignored")
        elif entry.event == "applied":
            execution[entry.thread_id] = "applied"
        elif entry.event in ("apply_failed", "apply_skipped"):
            execution.setdefault(entry.thread_id, "failed")

    records: dict[tuple[Action, Domain], TrackRecord] = {}
    for tid, key in scope.items():
        decision = outcome.get(tid)
        if decision not in ("approved", "edited", "rejected", "ignored"):
            continue  # still pending, or auto_applied
        record = records.setdefault(key, TrackRecord(action=key[0], domain=key[1]))
        setattr(record, decision, getattr(record, decision) + 1)
        if decision in ("approved", "edited"):
            status = execution.get(tid)
            if status == "applied":
                record.applied += 1
            else:
                # Missing evidence is failure evidence for graduation. Older
                # audit logs must earn a new, complete observation window.
                record.apply_failed += 1
    return records


def suggest_graduations(
    audit_log: Any,
    matrix: PermissionMatrix,
    *,
    window_days: int = 30,
    min_decisions: int = GRADUATION_MIN_DECISIONS,
    min_approval_rate: float = GRADUATION_MIN_APPROVAL_RATE,
    now: datetime | None = None,
) -> list[GraduationSuggestion]:
    """Suggestions — information only, never applied by code (rule 3).

    Bar (per (action, domain), over the window): at least ``min_decisions``
    human decisions, an unedited-approval rate at or above
    ``min_approval_rate``, zero rejections, a successful apply for every
    accepted proposal, and currently below ACT_NOTIFY.
    """
    suggestions: list[GraduationSuggestion] = []
    for key, record in track_records(
        audit_log, window_days=window_days, now=now
    ).items():
        if matrix.max_rung(*key) >= Rung.ACT_NOTIFY:
            continue
        if record.total < min_decisions or record.rejected > 0:
            continue
        accepted = record.approved + record.edited
        if record.apply_failed > 0 or record.applied < accepted:
            continue
        if record.approved / record.total < min_approval_rate:
            continue
        suggestions.append(GraduationSuggestion(record=record))
    return suggestions


# ---------------------------------------------------------------------------
# Demotion (Phase 4 item 5, docs/future-state.md) — graduation's mirror image
# ---------------------------------------------------------------------------


@dataclass
class DemotionSuggestion:
    """A rejection streak (or a single rejection of an auto-applied effect)
    against a granted (action, domain) at rung > PROPOSE — surfaced exactly
    like a graduation, but always targeting PROPOSE (never a gradual
    one-rung step: an auto-acting grant that produced rejections should
    drop all the way back to human-approval-per-item)."""

    action: Action
    domain: Domain
    from_rung: Rung
    scope: GrantScope | None = None
    to_rung: Rung = Rung.PROPOSE
    rejected: int = 0
    window: int = 0

    def render(self) -> str:
        scope_note = f" scoped to {render_scope(self.scope)}" if self.scope is not None else ""
        flags = _scope_cli_flags(self.scope)
        return (
            f"{self.rejected}/{self.window} recent {self.action.value} "
            f"decisions on {self.domain.value}{scope_note} at "
            f"{self.from_rung.name} were rejected — consider demoting to "
            f"{self.to_rung.name}: attune autonomy grant {self.action.value} "
            f"{self.domain.value} {self.to_rung.name.lower()}{flags}"
        )


def _scope_cli_flags(scope: GrantScope | None) -> str:
    """The ``--priority``/``--tier`` CLI flag suffix that reproduces
    ``scope`` exactly, for a demotion suggestion's rendered CLI command —
    demotion (unlike graduation) can target a SCOPED grant, so the
    suggested command must carry the scope back through, or a human
    copy-pasting it would re-grant the wrong (unscoped) thing."""
    if scope is None:
        return ""
    parts = []
    if scope.priorities is not None:
        parts.append(f"--priority {','.join(sorted(scope.priorities))}")
    if scope.tiers is not None:
        parts.append(f"--tier {','.join(sorted(scope.tiers))}")
    return (" " + " ".join(parts)) if parts else ""


def _recent_decisions(
    audit_log: Any, action: Action, domain: Domain, *, limit: int,
) -> list[tuple[str, str]]:
    """The last ``limit`` human decisions recorded for (action, domain),
    oldest-first trimmed to the most recent ``limit`` — reuses
    ``track_records``' event vocabulary (``autonomy_gate`` ties action/
    domain + ``routed_to`` to a thread_id; ``human_decision``/
    ``approval_ignored`` supply the decision) but windows by COUNT, not
    calendar time (see :data:`DEMOTION_WINDOW_DECISIONS`).

    Each returned entry is ``(decision, routed_to)`` — ``routed_to`` is
    "approve" or "auto_apply" per the gate's own audit event, letting
    :func:`suggest_demotions` tell an ordinary approval-card rejection
    apart from a rejection recorded against an auto-applied effect (a
    materially stronger signal).
    """
    gate: dict[str, tuple[Action, Domain, str, str]] = {}
    outcome: dict[str, str] = {}
    for entry in audit_log.query():
        if entry.event == "autonomy_gate":
            try:
                a = Action(entry.fields.get("action"))
                d = Domain(entry.fields.get("domain") or entry.domain)
            except ValueError:
                continue
            gate[entry.thread_id] = (a, d, entry.fields.get("routed_to", ""), entry.ts)
        elif entry.event == "human_decision":
            outcome[entry.thread_id] = entry.fields.get("decision", "")
        elif entry.event == "approval_ignored":
            outcome.setdefault(entry.thread_id, "ignored")

    rows = []
    for tid, (a, d, routed_to, ts) in gate.items():
        if (a, d) != (action, domain):
            continue
        decision = outcome.get(tid)
        if decision not in ("approved", "edited", "rejected", "ignored"):
            continue  # still pending, or auto_applied with no later decision
        rows.append((ts, decision, routed_to))
    rows.sort(key=lambda r: r[0])
    return [(decision, routed_to) for _, decision, routed_to in rows[-limit:]]


def suggest_demotions(
    audit_log: Any,
    matrix: PermissionMatrix,
    *,
    window_decisions: int = DEMOTION_WINDOW_DECISIONS,
    min_rejections: int = DEMOTION_MIN_REJECTIONS,
) -> list[DemotionSuggestion]:
    """Suggestions — information only, never applied by code (rule 3), same
    as :func:`suggest_graduations`.

    Examines EVERY grant entry held (scoped or not) at rung > PROPOSE —
    unlike graduation, which only ever operates on the unscoped grant, a
    demotion must be able to walk back a SCOPED grant too, since that's how
    ACT_NOTIFY/AUTONOMOUS autonomy is actually earned in practice (a CLI
    grant or an accepted graduation). Over the last ``window_decisions``
    human decisions recorded for that (action, domain) pair (there is no
    per-scope audit trail, so the window is shared across every grant on
    the pair): ``min_rejections`` or more rejections, OR any single
    rejection recorded against an auto-applied effect (``routed_to ==
    "auto_apply"`` — a materially stronger signal, since an auto-applied
    effect skipped human review entirely; the live graph cannot produce
    this combination today, since an auto-applied run never reaches
    ``human_decision`` at all, but the check is deliberately literal and
    defensive so a future affordance — e.g. "flag this auto-acted effect as
    wrong" — is already covered, see docs/decisions.md).
    """
    suggestions: list[DemotionSuggestion] = []
    for (action, domain), entries in matrix.grants.items():
        for sg in entries:
            if sg.rung <= Rung.PROPOSE:
                continue
            recent = _recent_decisions(
                audit_log, action, domain, limit=window_decisions
            )
            rejected = sum(1 for decision, _ in recent if decision == "rejected")
            auto_rejected = any(
                decision == "rejected" and routed_to == "auto_apply"
                for decision, routed_to in recent
            )
            if rejected >= min_rejections or auto_rejected:
                suggestions.append(DemotionSuggestion(
                    action=action, domain=domain, from_rung=sg.rung,
                    scope=sg.scope, rejected=rejected, window=len(recent),
                ))
    return suggestions


# ---------------------------------------------------------------------------
# Graduation/demotion approval cards (Phase 4 stage 2, G13) — the
# resolution counterpart to ``draft_approve.resume_workflow`` for cards
# that have no LangGraph workflow behind them at all.
# ---------------------------------------------------------------------------


def graduation_thread_id(action: Action, domain: Domain, to_rung: Rung) -> str:
    return f"{GRADUATION_PREFIX}{action.value}:{domain.value}:{to_rung.name.lower()}"


def demotion_thread_id(action: Action, domain: Domain, to_rung: Rung) -> str:
    return f"{DEMOTION_PREFIX}{action.value}:{domain.value}:{to_rung.name.lower()}"


class JsonGraduationState:
    """Small persisted state for graduation/demotion approval cards: a
    snapshot of exactly what each posted card would grant, and rejection
    cooldowns — both keyed by the card's own thread_id string, which is
    already unique per (kind, action, domain, to_rung).

    Two separate concerns, one file (guarded by ``fslock`` cross-process,
    like every other multi-writer state file in this codebase):

    - **card snapshots** (``record_card``/``get_card``/``remove_card``): a
      GrantScope (a pair of frozensets) can't round-trip through a bare
      thread-id string, so a demotion targeting a SCOPED grant needs this
      snapshot to know exactly what to re-grant when a human clicks
      approve. Written when the card is posted; removed once it resolves.
    - **rejection cooldowns** (``in_cooldown``/``record_rejection``):
      consulted by ``runtime.post_autonomy_digest`` BEFORE a new card is
      ever built for a given thread_id identity, independent of any one
      card's snapshot — a rejected suggestion must stay suppressed across
      many digest runs, long after its card's snapshot is gone.
    """

    def __init__(self, path: str):
        self._path = path

    def record_card(
        self, thread_id: str, *, kind: str, action: Action, domain: Domain,
        to_rung: Rung, scope: GrantScope | None = None,
    ) -> None:
        with locked(self._path + ".lock"):
            data = self._load()
            data.setdefault("cards", {})[thread_id] = {
                "kind": kind,
                "action": action.value,
                "domain": domain.value,
                "to_rung": int(to_rung),
                "scope": _scope_to_json(scope),
            }
            self._save(data)

    def get_card(self, thread_id: str) -> dict[str, Any] | None:
        raw = self._load().get("cards", {}).get(thread_id)
        if raw is None:
            return None
        return {
            "kind": raw["kind"],
            "action": Action(raw["action"]),
            "domain": Domain(raw["domain"]),
            "to_rung": Rung(int(raw["to_rung"])),
            "scope": _scope_from_json(raw.get("scope")),
        }

    def remove_card(self, thread_id: str) -> None:
        with locked(self._path + ".lock"):
            data = self._load()
            data.get("cards", {}).pop(thread_id, None)
            self._save(data)

    def in_cooldown(
        self, key: str, *, now: datetime,
        cooldown_days: int = GRADUATION_REJECTION_COOLDOWN_DAYS,
    ) -> bool:
        raw = self._load().get("rejections", {}).get(key)
        if raw is None:
            return False
        rejected_at = datetime.fromisoformat(raw["rejected_at"])
        return now - rejected_at < timedelta(days=cooldown_days)

    def record_rejection(self, key: str, *, at: datetime) -> None:
        with locked(self._path + ".lock"):
            data = self._load()
            data.setdefault("rejections", {})[key] = {
                "rejected_at": at.astimezone(timezone.utc).isoformat(),
            }
            self._save(data)

    def _load(self) -> dict[str, Any]:
        if not os.path.exists(self._path):
            return {}
        with open(self._path) as fh:
            return json.load(fh)

    def _save(self, data: dict[str, Any]) -> None:
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        temp = f"{self._path}.tmp"
        with open(temp, "w") as fh:
            json.dump(data, fh)
        os.replace(temp, self._path)


def resolve_autonomy_card(
    thread_id: str,
    decision: str,
    *,
    store: JsonPermissionMatrixStore,
    matrix: PermissionMatrix,
    cooldown_state: Any,
    audit_log: Any,
    user_id: str,
    pending: Any = None,
    actor: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resolve a human's decision on a graduation/demotion approval card
    (Phase 4 stage 2, G13) — the counterpart to
    ``draft_approve.resume_workflow`` for cards that have no LangGraph
    workflow behind them at all. Called from the SAME approve/reject/edit
    button handlers as every other approval card (Slack/Chat), routed here
    instead of a graph resume because the thread_id carries the
    "graduation:"/"demotion:" namespace — see ``runtime._bound_resume``.

    Approve/edit: applies ``grant(...)`` through the persisted store —
    audited, exactly what the CLI would do. Reject: persists a rejection
    with a 30-day re-suggestion cooldown. Either way the card's snapshot is
    removed and the pending entry (if any) is resolved/claimed, so the
    ignore-sweep never re-fires on an answered card.

    HARD CEILING re-checked HERE (defense in depth, docs/decisions.md): a
    forged or stale thread_id/card snapshot claiming SEND_REPLY or a rung
    above ACT_NOTIFY is refused outright, never granted — this is checked
    against the persisted snapshot, not just wherever the card was
    originally built, so a card that somehow got past the build-time check
    (or was built by an older/buggy version) still can't buy more autonomy
    than the ceiling allows.
    """
    now = now or datetime.now(timezone.utc)

    if pending is not None and hasattr(pending, "claim"):
        claimed = pending.claim(thread_id, actor=actor)
        if claimed is False:
            return {"card_kind": "autonomy", "resolution": "already_handled"}
    elif pending is not None:
        pending.resolve(thread_id)

    card = cooldown_state.get_card(thread_id) if cooldown_state is not None else None
    if card is None:
        return {
            "card_kind": "autonomy", "resolution": "unknown_card",
            "thread_id": thread_id,
        }

    kind, action, domain, to_rung, scope = (
        card["kind"], card["action"], card["domain"], card["to_rung"],
        card["scope"],
    )

    # The ceiling binds per kind. A GRADUATION raises authority, so the
    # excluded-action/max-rung ceiling applies. A DEMOTION only ever lowers
    # to PROPOSE — refusing it by action would block the human from
    # REDUCING a CLI-granted send's autonomy through the card, which is
    # backwards from safety. Its own defense-in-depth check is that the
    # snapshot's target rung really is a lowering one: a "demotion" card
    # claiming a target above PROPOSE is refused outright.
    over_ceiling = kind == "graduation" and (
        action in GRADUATION_CARD_EXCLUDED_ACTIONS
        or to_rung > GRADUATION_CARD_MAX_RUNG
    )
    if kind == "demotion" and to_rung > Rung.PROPOSE:
        over_ceiling = True
    if over_ceiling:
        cooldown_state.remove_card(thread_id)
        _audit_grant_event(
            audit_log, "autonomy_card_refused", action, domain, user_id,
            to_rung=int(to_rung), reason="ceiling",
        )
        return {
            "card_kind": "autonomy", "resolution": "refused_ceiling",
            "action": action.value, "domain": domain.value,
        }

    if decision in ("approved", "edited"):
        grant(
            store, matrix, action, domain, to_rung, scope=scope,
            audit_log=audit_log, user_id=user_id,
        )
        cooldown_state.remove_card(thread_id)
        return {
            "card_kind": "autonomy", "resolution": "granted",
            "action": action.value, "domain": domain.value,
            "to_rung": to_rung.name,
        }

    # rejected (or anything unrecognized defaults to rejected — fail safe)
    cooldown_state.record_rejection(thread_id, at=now)
    cooldown_state.remove_card(thread_id)
    _audit_grant_event(
        audit_log, "autonomy_card_rejected", action, domain, user_id,
        to_rung=int(to_rung),
    )
    return {
        "card_kind": "autonomy", "resolution": "rejected",
        "action": action.value, "domain": domain.value,
        "to_rung": to_rung.name,
    }


def parse_action(value: str) -> Action:
    """Strict: a typo must error, never silently default (contrast the
    graph's lenient `_as_action`, which guards runtime state, not user
    commands)."""
    return Action(value.strip().lower())


def parse_domain(value: str) -> Domain:
    return Domain(value.strip().lower())


def parse_rung(value: str) -> Rung:
    v = value.strip().upper()
    if v.isdigit():
        return Rung(int(v))
    return Rung[v]


def parse_scope(
    priority: str | None, tier: str | None
) -> GrantScope | None:
    """Strict CLI scope parsing (Phase 4 stage 1): comma-separated
    priority/tier lists, validated against the real enums and canonicalized
    to lowercase sets. Both absent (the common case) is the unscoped grant
    — ``None``. An empty value (``""`` or a stray ``","``) is a hard error,
    same posture as :func:`parse_action`/:func:`parse_domain`/
    :func:`parse_rung`: a scope that matches nothing is never useful and is
    almost certainly a typo."""
    priorities = _parse_scope_values(priority, Priority) if priority else None
    tiers = _parse_scope_values(tier, ImportanceTier) if tier else None
    if priorities is None and tiers is None:
        return None
    return GrantScope(priorities=priorities, tiers=tiers)


def _parse_scope_values(raw: str, enum_cls: Any) -> "frozenset[str]":
    values = frozenset(v.strip().lower() for v in raw.split(","))
    if not values or "" in values:
        raise ValueError(f"empty value in scope list: {raw!r}")
    for v in values:
        enum_cls(v)  # strict: raises ValueError on an unknown value
    return values


def _audit_grant_event(
    audit_log: Any,
    event: str,
    action: Action,
    domain: Domain,
    user_id: str,
    **fields: Any,
) -> None:
    if audit_log is None:
        return
    audit_log.record(
        thread_id=f"autonomy:{action.value}:{domain.value}",
        workflow="autonomy",
        events=[{
            "event": event,
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action.value,
            "domain": domain.value,
            **fields,
        }],
        domain=domain.value,
        user_id=user_id,
    )
