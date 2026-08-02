"""The weekly optimization job orchestrator (build prompt 36, task 1): ties
:mod:`.gepa` (``draft``) and :mod:`.mipro` (``triage``) to the promotion
gate (:mod:`.promotion`) and the versioned prompt registry (``prompts.py``)
into ONE completed run — candidate versions, per-scorer deltas, the
promotion decision, and (when promoted) the resulting prompt version id, all
in one :class:`OptimizationRunReport` (this build prompt's own acceptance
criterion: "one completed optimization run recorded end to end").

Never runs in the request path (a hard constraint): every collaborator here
(model clients, golden cases, prompts) is injected, so this module is
exactly as offline-testable as the eval harness (``evals.runner.run_eval``)
it builds on — a real weekly run only differs in which clients get passed
in (see ``cli/optimize_cmd.py``, which does that wiring, the same split
``cli/eval_cmd.py`` already holds for the eval harness itself).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from .. import prompts
from ..evals.schema import EvalCase
from ..evals.triage_eval import TriageCase
from .gepa import Candidate, run_gepa
from .promotion import PromotionDecision, evaluate_promotion
from .scoring import DraftScorecard
from .mipro import run_mipro


@dataclass(frozen=True)
class PromptRunOutcome:
    """One prompt's outcome within the weekly run."""

    name: str
    optimizer: str  # "gepa" | "mipro"
    baseline_version: int
    candidate_label: str
    decision: PromotionDecision | None
    promoted_version: int | None
    scorer_deltas: dict[str, float]
    note: str


@dataclass(frozen=True)
class OptimizationRunReport:
    run_at: str
    outcomes: tuple[PromptRunOutcome, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "run_at": self.run_at,
            "outcomes": [
                {
                    "name": o.name,
                    "optimizer": o.optimizer,
                    "baseline_version": o.baseline_version,
                    "candidate_label": o.candidate_label,
                    "approved": o.decision.approved if o.decision else None,
                    "reasons": list(o.decision.reasons) if o.decision else [],
                    "excluded_domains": list(o.decision.excluded_domains) if o.decision else [],
                    "promoted_version": o.promoted_version,
                    "scorer_deltas": dict(o.scorer_deltas),
                    "note": o.note,
                }
                for o in self.outcomes
            ],
        }


def _draft_deltas(candidate: DraftScorecard, baseline: DraftScorecard) -> dict[str, float]:
    deltas: dict[str, float] = {}
    if candidate.edit_burden_proxy is not None and baseline.edit_burden_proxy is not None:
        deltas["edit_burden_proxy"] = candidate.edit_burden_proxy - baseline.edit_burden_proxy
    if candidate.coverage_proxy is not None and baseline.coverage_proxy is not None:
        deltas["coverage_proxy"] = candidate.coverage_proxy - baseline.coverage_proxy
    if candidate.mean_win_rate is not None and baseline.mean_win_rate is not None:
        deltas["pairwise_win_rate"] = candidate.mean_win_rate - baseline.mean_win_rate
    return deltas


def _best_candidate(frontier: Sequence[Candidate]) -> Candidate | None:
    """The frontier member with the lowest (best) edit burden among those
    that measured one at all, ties broken by the higher mean pairwise win
    rate -- the single "recommended" promotion candidate the frontier's
    many tradeoffs collapse to when a caller needs exactly one."""
    measurable = [c for c in frontier if c.scorecard.edit_burden_proxy is not None]
    if not measurable:
        return None

    def _key(c: Candidate) -> tuple[float, float]:
        return (c.scorecard.edit_burden_proxy, -(c.scorecard.mean_win_rate or 0.0))  # type: ignore[arg-type]

    return min(measurable, key=_key)


def optimize_draft(
    *,
    cases: Sequence[EvalCase],
    candidate_fn_factory: Callable[[str], Callable[[EvalCase], str]],
    judge_client: Any,
    reflection_client: Any,
    agreement_by_domain: dict[str, float],
    versions_dir: str | None = None,
    rollout_budget: int = 200,
    minibatch_size: int = 8,
    seed: int = 0,
    now: datetime | None = None,
) -> PromptRunOutcome:
    base = prompts.current(prompts.PROMPT_DRAFT, versions_dir=versions_dir)
    if not cases:
        return PromptRunOutcome(
            name="draft", optimizer="gepa", baseline_version=base.version, candidate_label="none",
            decision=None, promoted_version=None, scorer_deltas={},
            note="no golden cases available -- excluded from optimization",
        )

    result = run_gepa(
        base_prefix=base.stable_prefix, cases=cases, candidate_fn_factory=candidate_fn_factory,
        judge_client=judge_client, reflection_client=reflection_client,
        agreement=agreement_by_domain, rollout_budget=rollout_budget,
        minibatch_size=minibatch_size, seed=seed,
    )
    non_baseline = [c for c in result.frontier if c.label != "baseline"]
    best = _best_candidate(non_baseline)
    if best is None:
        return PromptRunOutcome(
            name="draft", optimizer="gepa", baseline_version=base.version, candidate_label="none",
            decision=None, promoted_version=None, scorer_deltas={},
            note="no candidate improved on the baseline this run",
        )

    deltas = _draft_deltas(best.scorecard, result.baseline.scorecard)
    decision = evaluate_promotion(
        best.scorecard, result.baseline.scorecard, agreement_by_domain=agreement_by_domain,
    )
    promoted_version = None
    if decision.approved:
        promoted = prompts.promote(
            prompts.PROMPT_DRAFT, best.stable_prefix, source="gepa", scorer_deltas=deltas,
            note=f"promoted from GEPA candidate {best.label}", versions_dir=versions_dir, now=now,
        )
        promoted_version = promoted.version

    return PromptRunOutcome(
        name="draft", optimizer="gepa", baseline_version=base.version, candidate_label=best.label,
        decision=decision, promoted_version=promoted_version, scorer_deltas=deltas,
        note="; ".join(result.log[-3:]),
    )


def optimize_triage(
    *,
    cases: Sequence[TriageCase],
    triage_fn_factory: Callable[[str], Callable[[TriageCase], Any]],
    instruction_client: Any,
    versions_dir: str | None = None,
    n_candidates: int = 4,
    now: datetime | None = None,
) -> PromptRunOutcome:
    base = prompts.current(prompts.PROMPT_TRIAGE, versions_dir=versions_dir)
    if not cases:
        return PromptRunOutcome(
            name="triage", optimizer="mipro", baseline_version=base.version, candidate_label="none",
            decision=None, promoted_version=None, scorer_deltas={},
            note="no golden cases available -- excluded from optimization",
        )

    result = run_mipro(
        base_prefix=base.stable_prefix, cases=cases, triage_fn_factory=triage_fn_factory,
        instruction_client=instruction_client, n_candidates=n_candidates,
    )
    deltas = {"triage_accuracy": result.best_score - result.baseline_score}
    if result.best_prefix.strip() == base.stable_prefix.strip():
        return PromptRunOutcome(
            name="triage", optimizer="mipro", baseline_version=base.version, candidate_label="none",
            decision=None, promoted_version=None, scorer_deltas=deltas,
            note=f"MIPRO tried {result.candidates_tried} candidate(s); none beat the baseline",
        )

    approved = result.best_score > result.baseline_score
    decision = PromotionDecision(
        approved=approved,
        reasons=() if approved else (
            f"no improvement (score {result.baseline_score:.3f} -> {result.best_score:.3f})",
        ),
        excluded_domains=(),
    )
    promoted_version = None
    if decision.approved:
        promoted = prompts.promote(
            prompts.PROMPT_TRIAGE, result.best_prefix, source="mipro", scorer_deltas=deltas,
            note=f"promoted from MIPRO ({result.candidates_tried} candidate(s) tried)",
            versions_dir=versions_dir, now=now,
        )
        promoted_version = promoted.version

    return PromptRunOutcome(
        name="triage", optimizer="mipro", baseline_version=base.version,
        candidate_label="mipro-best" if approved else "none",
        decision=decision, promoted_version=promoted_version, scorer_deltas=deltas,
        note=f"MIPRO tried {result.candidates_tried} candidate(s)",
    )


def run_weekly_optimization(
    *,
    draft_cases: Sequence[EvalCase] = (),
    draft_candidate_fn_factory: Callable[[str], Callable[[EvalCase], str]] | None = None,
    judge_client: Any = None,
    reflection_client: Any = None,
    triage_cases: Sequence[TriageCase] = (),
    triage_fn_factory: Callable[[str], Callable[[TriageCase], Any]] | None = None,
    instruction_client: Any = None,
    agreement_by_domain: dict[str, float] | None = None,
    versions_dir: str | None = None,
    rollout_budget: int = 200,
    minibatch_size: int = 8,
    n_candidates: int = 4,
    seed: int = 0,
    now: datetime | None = None,
) -> OptimizationRunReport:
    """The full weekly run. A prompt whose required collaborators weren't
    passed (e.g. no reflection client configured) is simply omitted from
    ``outcomes`` -- an incompletely-configured job still records everything
    it COULD run, rather than failing the whole pass."""
    now = now or datetime.now(timezone.utc)
    agreement_by_domain = agreement_by_domain or {}
    outcomes: list[PromptRunOutcome] = []

    if draft_candidate_fn_factory is not None and judge_client is not None and reflection_client is not None:
        outcomes.append(optimize_draft(
            cases=draft_cases, candidate_fn_factory=draft_candidate_fn_factory,
            judge_client=judge_client, reflection_client=reflection_client,
            agreement_by_domain=agreement_by_domain, versions_dir=versions_dir,
            rollout_budget=rollout_budget, minibatch_size=minibatch_size, seed=seed, now=now,
        ))
    if triage_fn_factory is not None and instruction_client is not None:
        outcomes.append(optimize_triage(
            cases=triage_cases, triage_fn_factory=triage_fn_factory,
            instruction_client=instruction_client, versions_dir=versions_dir,
            n_candidates=n_candidates, now=now,
        ))

    return OptimizationRunReport(run_at=now.isoformat(), outcomes=tuple(outcomes))
