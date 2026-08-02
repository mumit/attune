"""The promotion gate (build prompt 36, tasks 3–5) — including the two
acceptance-mandated tests: a coverage-reducing candidate is rejected, and a
domain below the judge-agreement threshold is excluded from optimization."""

from __future__ import annotations

from attune.evals.agreement import AGREEMENT_THRESHOLD
from attune.evals.report import DomainPairwise
from attune.optimize.promotion import evaluate_promotion
from attune.optimize.scoring import DraftScorecard


def _scorecard(*, edit_burden, coverage, win_rate=0.5, domain="mail", gates=True, agreement=0.9, total=10):
    wins = round(win_rate * total)
    return DraftScorecard(
        edit_burden_proxy=edit_burden,
        pairwise=(DomainPairwise(domain, total, wins, total - wins, 0, 0, 0.0, agreement, gates),),
        coverage_proxy=coverage,
    )


def test_genuine_improvement_with_flat_coverage_is_approved():
    baseline = _scorecard(edit_burden=0.30, coverage=0.90)
    candidate = _scorecard(edit_burden=0.20, coverage=0.90)
    decision = evaluate_promotion(candidate, baseline, agreement_by_domain={"mail": 0.9})
    assert decision.approved
    assert decision.reasons == ()


def test_coverage_reducing_candidate_is_rejected_even_with_edit_burden_improvement():
    """The acceptance-mandated test: the optimizer must not be allowed to
    trade coverage for a better edit-burden number -- the exact RLUF failure
    mode (docs/landscape-2026.md §5)."""
    baseline = _scorecard(edit_burden=0.30, coverage=0.90)
    candidate = _scorecard(edit_burden=0.05, coverage=0.40)  # much "better" edit burden, much worse coverage

    decision = evaluate_promotion(candidate, baseline, agreement_by_domain={"mail": 0.9})

    assert not decision.approved
    assert any("coverage_proxy fell" in r for r in decision.reasons)


def test_domain_below_agreement_threshold_is_excluded_from_optimization():
    """The other acceptance-mandated test: an unmeasured or untrustworthy
    domain must be reported as excluded, not silently treated as passing."""
    baseline = _scorecard(edit_burden=0.30, coverage=0.90, domain="calendar", gates=False, agreement=0.5)
    candidate = _scorecard(edit_burden=0.20, coverage=0.90, domain="calendar", gates=False, agreement=0.5)

    decision = evaluate_promotion(
        candidate, baseline, agreement_by_domain={"calendar": AGREEMENT_THRESHOLD - 0.1},
    )
    assert decision.excluded_domains == ("calendar",)


def test_unmeasured_domain_is_excluded_same_as_a_known_bad_one():
    baseline = _scorecard(edit_burden=0.30, coverage=0.90, domain="slack", gates=False)
    candidate = _scorecard(edit_burden=0.20, coverage=0.90, domain="slack", gates=False)

    decision = evaluate_promotion(candidate, baseline, agreement_by_domain={})  # no record at all
    assert decision.excluded_domains == ("slack",)


def test_domain_at_or_above_threshold_is_not_excluded():
    baseline = _scorecard(edit_burden=0.30, coverage=0.90, domain="mail", gates=True, agreement=0.9)
    candidate = _scorecard(edit_burden=0.20, coverage=0.90, domain="mail", gates=True, agreement=0.9)

    decision = evaluate_promotion(candidate, baseline, agreement_by_domain={"mail": AGREEMENT_THRESHOLD})
    assert decision.excluded_domains == ()


def test_no_improvement_on_the_north_star_is_rejected():
    baseline = _scorecard(edit_burden=0.20, coverage=0.90)
    candidate = _scorecard(edit_burden=0.20, coverage=0.90)  # identical, not an improvement
    decision = evaluate_promotion(candidate, baseline, agreement_by_domain={"mail": 0.9})
    assert not decision.approved
    assert any("no improvement on the north star" in r for r in decision.reasons)


def test_regression_budget_violation_on_a_gating_domain_is_reused():
    baseline = _scorecard(edit_burden=0.30, coverage=0.90, win_rate=0.80, gates=True, agreement=0.9)
    candidate = _scorecard(edit_burden=0.20, coverage=0.90, win_rate=0.10, gates=True, agreement=0.9)  # huge pairwise regression
    decision = evaluate_promotion(candidate, baseline, agreement_by_domain={"mail": 0.9})
    assert not decision.approved
    assert any("pairwise win rate" in r for r in decision.reasons)


def test_regression_on_a_non_gating_domain_never_blocks_promotion():
    baseline = _scorecard(edit_burden=0.30, coverage=0.90, win_rate=0.80, gates=False, agreement=0.5)
    candidate = _scorecard(edit_burden=0.20, coverage=0.90, win_rate=0.10, gates=False, agreement=0.5)
    decision = evaluate_promotion(candidate, baseline, agreement_by_domain={"mail": 0.5})
    assert decision.approved
