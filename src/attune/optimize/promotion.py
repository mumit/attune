"""The promotion gate (build prompt 36, tasks 3–5): a candidate prompt
version is promoted only when the eval harness shows a genuine improvement
on the north star, no regression beyond budget on the scorers the harness
already tracks, and it survives two HARD, non-budgeted constraints this
module adds on top.

**The north star and coverage are judge-free, so the 75%-agreement gate
does not apply to them.** ``edit_burden_proxy`` (``orchestrator.ledger.
compute_edit_metrics``) is a deterministic Levenshtein-style distance, and
:mod:`.coverage`'s proxy is a length check — neither ever calls an LLM
judge. Only the PAIRWISE win rate depends on the judge, and that is exactly
what ``evals.report.DomainPairwise.gates`` already carries per domain (build
prompt 27, task 3) and ``evals.ci_gate.check_regression_budget`` already
skips for a non-gating domain. This module's :func:`evaluate_promotion`
reuses that budget check unchanged (rather than re-deriving it) and adds
only the two checks the existing harness has no opinion on:

- **Task 4**: a domain whose judge-agreement is unmeasured or below
  :data:`~..evals.agreement.AGREEMENT_THRESHOLD` is reported as excluded —
  informational here (the regression-budget diff already can't fail on it,
  since its ``DomainPairwise.gates`` is already ``False`` when the caller
  built the scorecard with the same ``agreement_by_domain``), surfaced so
  the run report can say so explicitly rather than silently.
- **Task 5**: a candidate whose coverage proxy FELL is rejected outright,
  regardless of any edit-burden gain — the RLUF guardrail
  (``docs/landscape-2026.md`` §5): "an assistant that drafts only the easy
  replies and stays silent on the hard ones."

Trajectory assertions (``evals.trajectory``) are deliberately NOT re-run
here: every one of them checks a structural invariant (capability
selection, autonomy rung, retrieval score floor) that a prompt's WORDING
cannot change, and every promotion lands as a pull request (task 3) that
ordinary CI already runs the full ``evals`` suite — trajectory assertions
included — against. Re-running them here would duplicate what CI already
guarantees on the same PR; see ``docs/decisions.md``.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..evals.agreement import domain_gates
from ..evals.ci_gate import check_regression_budget
from ..evals.report import EvalReport
from .scoring import DraftScorecard


@dataclass(frozen=True)
class PromotionDecision:
    approved: bool
    reasons: tuple[str, ...]
    excluded_domains: tuple[str, ...]


def _as_eval_report(scorecard: DraftScorecard) -> EvalReport:
    """Wrap a :class:`~.scoring.DraftScorecard` in the shape
    ``evals.ci_gate.check_regression_budget`` already knows how to diff —
    reuse, not a parallel regression-budget implementation."""
    return EvalReport(
        edit_burden_proxy=scorecard.edit_burden_proxy,
        pairwise=scorecard.pairwise, triage=None, injection=None, generated_at="",
    )


def evaluate_promotion(
    candidate: DraftScorecard,
    baseline: DraftScorecard,
    *,
    agreement_by_domain: dict[str, float] | None = None,
    regression_budget: dict[str, float] | None = None,
) -> PromotionDecision:
    """Whether ``candidate`` may replace ``baseline`` as the promoted
    prompt. Every reason a rejection fires is collected, not just the
    first, so a rejected candidate's full diagnostic lands in the run
    report rather than a bare boolean."""
    agreement_by_domain = agreement_by_domain or {}
    reasons: list[str] = []

    domains = {p.domain for p in candidate.pairwise} | {p.domain for p in baseline.pairwise}
    excluded = tuple(d for d in sorted(domains) if not domain_gates(agreement_by_domain, d))

    if (
        candidate.coverage_proxy is not None
        and baseline.coverage_proxy is not None
        and candidate.coverage_proxy < baseline.coverage_proxy
    ):
        reasons.append(
            f"coverage_proxy fell ({baseline.coverage_proxy:.3f} -> "
            f"{candidate.coverage_proxy:.3f}) -- rejected regardless of any edit-burden gain"
        )

    if candidate.edit_burden_proxy is None or baseline.edit_burden_proxy is None:
        reasons.append("edit_burden_proxy not measurable for candidate and/or baseline")
    elif not (candidate.edit_burden_proxy < baseline.edit_burden_proxy):
        reasons.append(
            f"no improvement on the north star (edit_burden_proxy "
            f"{baseline.edit_burden_proxy:.3f} -> {candidate.edit_burden_proxy:.3f})"
        )

    reasons.extend(check_regression_budget(_as_eval_report(candidate), _as_eval_report(baseline), regression_budget))

    return PromotionDecision(approved=not reasons, reasons=tuple(reasons), excluded_domains=excluded)
