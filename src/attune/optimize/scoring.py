"""Candidate-prefix scoring shared by the reflective optimizer (:mod:`.gepa`)
and the promotion gate (:mod:`.promotion`) — build prompt 36, task 1.

Deliberately a SEPARATE single pass from ``evals.runner.run_eval``, not a
call to it: GEPA needs the raw per-case trajectory (which case lost, what the
candidate actually said) to reflect on, and the coverage proxy needs to know
per-case whether an EDIT-kind draft was substantive -- both are naturally
computed in the SAME loop that already calls ``candidate_fn`` once per case
for pairwise judging, so folding them in here avoids invoking a candidate
prefix (a real model call, in a live run) twice per case. The aggregation
math itself (:func:`score_trajectories`) intentionally mirrors ``run_eval``'s
so a candidate's :class:`DraftScorecard` and the CI harness's
``EvalReport`` agree on what "pairwise win rate" and "edit_burden_proxy" mean.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from ..evals.agreement import domain_gates
from ..evals.judge import PairwiseResult, judge_pairwise
from ..evals.report import DomainPairwise
from ..evals.schema import CaseKind, EvalCase
from ..orchestrator.ledger import compute_edit_metrics
from .coverage import is_substantive


@dataclass(frozen=True)
class Trajectory:
    """One case's sampled outcome under a candidate prefix -- the unit GEPA's
    reflection step reads."""

    case: EvalCase
    candidate_text: str
    judge_result: PairwiseResult


@dataclass(frozen=True)
class DraftScorecard:
    """One candidate ``draft`` prefix's measured quality: the same shape the
    eval harness's ``EvalReport`` reports (so the promotion gate can compare
    apples to apples), plus the coverage proxy build prompt 36 requires."""

    edit_burden_proxy: float | None
    pairwise: tuple[DomainPairwise, ...]
    coverage_proxy: float | None

    @property
    def mean_win_rate(self) -> float | None:
        rates = [p.win_rate for p in self.pairwise if p.win_rate is not None]
        return (sum(rates) / len(rates)) if rates else None


def sample_trajectories(
    cases: Sequence[EvalCase],
    candidate_fn: Callable[[EvalCase], str],
    judge_client: Any,
    *,
    seed: int = 0,
) -> list[Trajectory]:
    """Run ``candidate_fn`` once per case and judge it pairwise against gold.
    The raw per-case record GEPA reflects on; :func:`score_trajectories`
    aggregates the same records into a :class:`DraftScorecard`."""
    rng = random.Random(seed)
    trajectories: list[Trajectory] = []
    for case in cases:
        candidate_text = candidate_fn(case)
        result = judge_pairwise(
            judge_client,
            case_id=case.case_id,
            context=case.inputs.get("incoming_summary", ""),
            candidate_text=candidate_text,
            gold_text=case.gold_text,
            rng=rng,
        )
        trajectories.append(Trajectory(case=case, candidate_text=candidate_text, judge_result=result))
    return trajectories


def losing_trajectories(trajectories: Sequence[Trajectory]) -> list[Trajectory]:
    """Cases where the candidate lost to what the human actually sent (or,
    for a REJECT case, lost to "no reply") -- GEPA's reflection input."""
    return [t for t in trajectories if t.judge_result.reported_winner == "gold"]


def score_trajectories(
    trajectories: Sequence[Trajectory],
    *,
    agreement: dict[str, float] | None = None,
) -> DraftScorecard:
    agreement = agreement or {}
    edit_distances: list[float] = []
    edit_substantive = 0
    edit_total = 0
    by_domain: dict[str, list[PairwiseResult]] = {}
    for t in trajectories:
        if t.case.kind is CaseKind.EDIT:
            edit_total += 1
            if is_substantive(t.candidate_text):
                edit_substantive += 1
            edit_distances.append(
                compute_edit_metrics(t.candidate_text, t.case.gold_text).distance_normalized
            )
        by_domain.setdefault(t.case.domain, []).append(t.judge_result)

    pairwise = []
    for domain, results in by_domain.items():
        wins = sum(1 for r in results if r.reported_winner == "candidate")
        losses = sum(1 for r in results if r.reported_winner == "gold")
        ties = sum(1 for r in results if r.reported_winner == "tie")
        unresolved = sum(1 for r in results if r.reported_winner is None)
        pairwise.append(DomainPairwise(
            domain=domain, total=len(results), wins=wins, losses=losses, ties=ties,
            unresolved=unresolved,
            disagreement_rate=(unresolved / len(results)) if results else 0.0,
            agreement_rate=agreement.get(domain),
            gates=domain_gates(agreement, domain),
        ))

    return DraftScorecard(
        edit_burden_proxy=(sum(edit_distances) / len(edit_distances)) if edit_distances else None,
        pairwise=tuple(pairwise),
        coverage_proxy=(edit_substantive / edit_total) if edit_total else None,
    )


def score_draft_candidate(
    cases: Sequence[EvalCase],
    candidate_fn: Callable[[EvalCase], str],
    judge_client: Any,
    *,
    agreement: dict[str, float] | None = None,
    seed: int = 0,
) -> DraftScorecard:
    return score_trajectories(
        sample_trajectories(cases, candidate_fn, judge_client, seed=seed), agreement=agreement,
    )
