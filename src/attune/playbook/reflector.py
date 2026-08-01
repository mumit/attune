"""The nightly reflector (build prompt 29, task 4): per-bullet accounting,
retirement/decay, and capped new-bullet proposal — extending the existing
consolidation job (``runtime.py``'s ``run_consolidation``) rather than
adding a second nightly pass.

Run in this order, always (task 4's own ordering — accounting must happen
before retirement, which must happen before proposal, every single run):

1. :func:`record_ledger_outcomes` — per-bullet ``helped``/``harmed``
   accounting from decided ledger rows whose ``context_attribution``
   named this bullet. This is the part ACE needs and the part RIZZ
   identifies as required to stop accumulated rules from interfering with
   each other, so it runs first.
2. :func:`retire_bullets` — ``harmed > helped`` auto-retirement over a
   minimum sample, plus 90-day decay for anything unused and unpinned.
3. :func:`propose_bullets` — new bullets from repeated edit/rejection
   evidence, hard-capped at :data:`~.bullets.MAX_NEW_BULLETS_PER_DAY`.

**The untrusted-content firewall (the highest-severity constraint in the
whole build prompt) is enforced structurally, not by instruction.**
:func:`propose_bullets` never reads an evidence record's free-text ``text``
field at all — only its ``metadata`` (``domain``, ``sender``, ``category``,
``decision``, ``proposal_id``), and ``category`` is always one of a closed
three-value vocabulary (``"formal"``/``"casual"``/``"neutral"``) computed by
:func:`classify_register` over the ASSISTANT'S OWN drafted text at capture
time (``memory.signals.capture_reflection_evidence``), never over an inbound
message body. A proposed bullet's text is always a closed template filled
from that categorical metadata plus a fenced sender — there is no code path
by which arbitrary free text (an inbound body, or an attacker's "add this to
your playbook" instruction) can reach a bullet's text, because nothing here
ever concatenates free text into one. Body text is unavailable to this
module's input assembly, by construction, not merely instructed against.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from .bullets import DOMAINS, MAX_NEW_BULLETS_PER_DAY, GitPlaybookStore

logger = logging.getLogger(__name__)

RETIRE_MIN_SAMPLE = 3
BULLET_DECAY_DAYS = 90
MIN_EVIDENCE_FOR_PROPOSAL = 3

_DECIDED_STATES = ("approved", "edited", "rejected")

_FORMAL_MARKERS = (
    "dear ", "to whom it may concern", "sincerely", "regards,",
    "best regards", "yours truly", "kind regards",
)
_CASUAL_MARKERS = ("hey", "hiya", "thanks!", "cheers", "!", "yep", "sounds good")


def classify_register(text: str | None) -> str:
    """A coarse, deterministic register classifier over the ASSISTANT'S OWN
    text — never a model call, never applied to inbound content anywhere in
    this codebase. Mirrors ``orchestrator.ledger``'s own deterministic
    ``_tone_signature`` proxy: a cheap heuristic proxy is exactly right here
    (this becomes the ONLY thing the reflector's proposal step is ever
    allowed to know about a draft's content), not a model judgment call."""
    t = (text or "").lower()
    formal_hits = sum(1 for m in _FORMAL_MARKERS if m in t)
    casual_hits = sum(1 for m in _CASUAL_MARKERS if m in t)
    if formal_hits > casual_hits and formal_hits > 0:
        return "formal"
    if casual_hits > formal_hits and casual_hits > 0:
        return "casual"
    return "neutral"


@dataclass(frozen=True)
class NewBulletProposal:
    domain: str
    text: str
    provenance: tuple[str, ...]


@dataclass
class ReflectionReport:
    """One nightly run's outcome — audited the same way
    ``ConsolidationReport`` already is."""

    accounted: int = 0
    retired: list[str] = field(default_factory=list)
    proposed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def record_ledger_outcomes(playbook: GitPlaybookStore, rows: Sequence[Any]) -> int:
    """Task 4, step 1: for every DECIDED row whose ``context_attribution``
    named at least one playbook bullet, increment that bullet's ``helped``
    (a clean approval) or ``harmed`` (an edit or a rejection — task 4's own
    wording: "increment helped (clean approval) or harmed (edit or
    rejection)"). Batched into one commit per domain via
    :meth:`GitPlaybookStore.record_outcomes_batch`. Returns the number of
    (row, bullet) accounting updates applied.
    """
    deltas_by_domain: dict[str, dict[str, list[int]]] = defaultdict(dict)
    applied = 0
    for row in rows:
        decision = getattr(row, "decision", None)
        if decision not in _DECIDED_STATES:
            continue
        attribution = getattr(row, "context_attribution", None)
        bullet_ids = getattr(attribution, "playbook_bullet_ids", ()) or ()
        if not bullet_ids:
            continue
        domain = getattr(row, "domain", None)
        if not domain:
            continue
        outcome_helped = decision == "approved"
        for bullet_id in bullet_ids:
            entry = deltas_by_domain[domain].setdefault(bullet_id, [0, 0])
            if outcome_helped:
                entry[0] += 1
            else:
                entry[1] += 1
            applied += 1

    for domain, deltas in deltas_by_domain.items():
        playbook.record_outcomes_batch(
            domain, {bid: (h, m) for bid, (h, m) in deltas.items()}
        )
    return applied


def retire_bullets(playbook: GitPlaybookStore, *, now: datetime | None = None) -> list[str]:
    """Task 4, step 2: ``harmed > helped`` auto-retirement over a minimum
    sample, and decay for anything unused past :data:`BULLET_DECAY_DAYS` —
    the same 90-day window ``orchestrator.importance`` already uses for its
    own decay. Pinned bullets are exempt from both (the principal's
    explicit override, ``attune playbook pin``)."""
    now = now or datetime.now(timezone.utc)
    retired: list[str] = []
    for domain in DOMAINS:
        for bullet in playbook.load_active(domain):
            if bullet.pinned:
                continue
            sample = bullet.helped + bullet.harmed
            if sample >= RETIRE_MIN_SAMPLE and bullet.harmed > bullet.helped:
                playbook.retire_bullet(
                    bullet.id,
                    reason=f"harmed({bullet.harmed}) > helped({bullet.helped}) over {sample} decisions",
                    now=now,
                )
                retired.append(bullet.id)
                continue
            last_seen = bullet.last_used_at or bullet.created_at
            if now - last_seen >= timedelta(days=BULLET_DECAY_DAYS):
                playbook.retire_bullet(
                    bullet.id,
                    reason=f"unused for {BULLET_DECAY_DAYS}+ days",
                    now=now,
                )
                retired.append(bullet.id)
    return retired


def _render_bullet_text(*, sender: str | None, category: str, decision: str, count: int) -> str:
    """A CLOSED TEMPLATE, filled only from categorical metadata — never from
    any evidence record's free text. See the module docstring: this is the
    structural half of the untrusted-content firewall. ``sender`` (when
    present) is fenced exactly like every other attacker-influenced field
    this codebase surfaces into a prompt (``memory.signals._fence_field``'s
    convention, reproduced here to avoid importing a private helper)."""
    verb = "rejected" if decision == "rejected" else "edited"
    if category == "formal":
        tone = "a less formal, more casual"
    elif category == "casual":
        tone = "a more formal, more polished"
    else:
        tone = "a different"
    recipient = f" to [UNTRUSTED-FIELD]{sender}[/UNTRUSTED-FIELD]" if sender else ""
    return (
        f"Prefer {tone} register in replies{recipient} — the principal "
        f"{verb} {count} {category}-register drafts in a row."
    )[:280]


def propose_bullets(
    evidence: Sequence[Any],
    *,
    existing_provenance: "set[str] | frozenset[str]" = frozenset(),
    max_new: int = MAX_NEW_BULLETS_PER_DAY,
) -> list[NewBulletProposal]:
    """Task 4, step 3: group reflection-evidence records by
    ``(domain, sender, category, decision)`` and turn any group with at
    least :data:`MIN_EVIDENCE_FOR_PROPOSAL` members into ONE new bullet,
    hard-capped at ``max_new``. ``evidence`` is whatever
    ``MemoryStore.get_all``/``search`` returned, duck-typed (``.metadata``)
    so this module never imports ``memory.base`` — only records whose
    ``metadata["signal"] == "reflection_evidence"`` are ever considered;
    everything else (including a forged record an attacker somehow got
    written with a different signal) is silently ignored.

    Deliberately reads ONLY ``record.metadata`` below, never
    ``record.text`` — see the module docstring for why that is the
    structural enforcement of "no bullet may derive from an inbound body."
    """
    if max_new <= 0:
        return []

    groups: dict[tuple[str, str, str, str], list[Any]] = defaultdict(list)
    for record in evidence:
        meta = getattr(record, "metadata", None) or {}
        if meta.get("signal") != "reflection_evidence":
            continue
        decision = meta.get("decision")
        if decision not in ("edited", "rejected"):
            continue
        proposal_id = meta.get("proposal_id")
        if not proposal_id or proposal_id in existing_provenance:
            continue
        category = meta.get("category", "neutral")
        if category == "neutral":
            continue  # nothing distinctive enough to generalize into a rule
        domain = meta.get("domain", "")
        sender = meta.get("sender") or ""
        key = (domain, sender, category, decision)
        groups[key].append(meta)

    proposals: list[NewBulletProposal] = []
    # Deterministic order (largest, most-recent-evidenced group first) so a
    # capped run always proposes the same bullets given the same evidence,
    # rather than depending on dict/set iteration order.
    ordered_keys = sorted(groups, key=lambda k: (-len(groups[k]), k))
    for domain, sender, category, decision in ordered_keys:
        if len(proposals) >= max_new:
            break
        metas = groups[(domain, sender, category, decision)]
        if len(metas) < MIN_EVIDENCE_FOR_PROPOSAL:
            continue
        provenance = tuple(sorted({m["proposal_id"] for m in metas}))
        text = _render_bullet_text(
            sender=sender or None, category=category, decision=decision,
            count=len(metas),
        )
        proposals.append(NewBulletProposal(domain=domain, text=text, provenance=provenance))
    return proposals


def run_nightly_reflection(
    playbook: GitPlaybookStore,
    *,
    ledger_rows: Sequence[Any] = (),
    evidence: Sequence[Any] = (),
    now: datetime | None = None,
) -> ReflectionReport:
    """The full nightly pass, in the mandated order (task 4). Best-effort at
    the call site (``runtime.py``): a playbook failure must never break the
    memory-consolidation pass it now rides alongside."""
    now = now or datetime.now(timezone.utc)
    report = ReflectionReport()

    report.accounted = record_ledger_outcomes(playbook, ledger_rows)
    report.retired = retire_bullets(playbook, now=now)

    already_today = playbook.count_created_on(now)
    remaining_cap = max(0, MAX_NEW_BULLETS_PER_DAY - already_today)
    if remaining_cap <= 0:
        report.notes.append(
            f"daily new-bullet cap already reached ({already_today}/"
            f"{MAX_NEW_BULLETS_PER_DAY}) — no proposals this run"
        )
        return report

    existing_provenance = playbook.all_provenance_ids()
    candidates = propose_bullets(
        evidence, existing_provenance=existing_provenance, max_new=remaining_cap,
    )
    for candidate in candidates:
        bullet = playbook.add_bullet(
            candidate.domain, candidate.text,
            provenance=candidate.provenance, now=now,
        )
        if bullet is not None:
            report.proposed.append(bullet.id)
    return report
