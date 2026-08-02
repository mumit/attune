"""GEPA-lite: the reflective optimizer over the ``draft`` prompt (build
prompt 36, tasks 1/2)."""

from __future__ import annotations

from datetime import datetime, timezone

from attune.evals.offline_fakes import deterministic_judge_client
from attune.evals.schema import CaseKind, EvalCase
from attune.optimize.gepa import (
    Candidate,
    _parse_reflection,
    dominates,
    merge,
    pareto_frontier,
    reflect,
    run_gepa,
)
from attune.optimize.offline_fakes import (
    MAGIC_MARKER,
    deterministic_merge_client,
    deterministic_reflection_client,
    prefix_sensitive_draft_fn_factory,
)
from attune.optimize.scoring import DraftScorecard, Trajectory


def _case(case_id: str, i: int) -> EvalCase:
    return EvalCase(
        case_id=case_id, kind=CaseKind.EDIT, domain="mail", action="draft_reply",
        inputs={"incoming_summary": f"Can you send the report by Friday? case {i}"},
        retrieved_context_ids=(), prompt_version=1,
        proposed_text="old draft",
        gold_text=f"Sure, I will send the report by Friday. (case {i})",
        captured_at=datetime.now(timezone.utc),
    )


def _scorecard(edit_burden, coverage, win_rate) -> DraftScorecard:
    from attune.evals.report import DomainPairwise

    wins = round(win_rate * 10)
    return DraftScorecard(
        edit_burden_proxy=edit_burden,
        pairwise=(DomainPairwise("mail", 10, wins, 10 - wins, 0, 0, 0.0, 0.9, True),),
        coverage_proxy=coverage,
    )


# ---------------------------------------------------------------------------
# Pareto dominance
# ---------------------------------------------------------------------------


def test_strictly_better_on_every_axis_dominates():
    better = _scorecard(edit_burden=0.1, coverage=0.9, win_rate=0.8)
    worse = _scorecard(edit_burden=0.3, coverage=0.7, win_rate=0.5)
    assert dominates(better, worse)
    assert not dominates(worse, better)


def test_mixed_tradeoff_does_not_dominate_either_way():
    a = _scorecard(edit_burden=0.1, coverage=0.5, win_rate=0.5)  # better edit burden
    b = _scorecard(edit_burden=0.3, coverage=0.9, win_rate=0.5)  # better coverage
    assert not dominates(a, b)
    assert not dominates(b, a)


def test_pareto_frontier_keeps_non_dominated_candidates():
    a = Candidate("a", "prefix a", None, _scorecard(0.1, 0.5, 0.5))
    b = Candidate("b", "prefix b", None, _scorecard(0.3, 0.9, 0.5))  # complementary tradeoff, survives
    c = Candidate("c", "prefix c", None, _scorecard(0.5, 0.1, 0.1))  # dominated by both
    frontier = pareto_frontier([a, b, c])
    assert {cand.label for cand in frontier} == {"a", "b"}


# ---------------------------------------------------------------------------
# reflect() / merge() parsing
# ---------------------------------------------------------------------------


def test_parse_reflection_extracts_diagnosis_and_revised_prefix():
    text = "DIAGNOSIS: too terse\nREVISED_PREFIX:\nBe warmer and more specific."
    result = _parse_reflection(text, fallback_prefix="fallback")
    assert result.diagnosis == "too terse"
    assert result.revised_prefix == "Be warmer and more specific."


def test_parse_reflection_falls_back_on_malformed_response():
    result = _parse_reflection("not the expected shape at all", fallback_prefix="unchanged")
    assert result.revised_prefix == "unchanged"


def test_reflect_appends_the_marker_via_deterministic_client():
    case = _case("c1", 1)
    trajectory = Trajectory(case=case, candidate_text="a generic reply", judge_result=None)  # type: ignore[arg-type]
    result = reflect(
        deterministic_reflection_client(), current_prefix="Draft a reply.", losses=[trajectory],
    )
    assert MAGIC_MARKER in result.revised_prefix
    assert "Draft a reply." in result.revised_prefix


def test_merge_combines_distinct_lines_from_both_prefixes():
    a = Candidate("a", "line one\nline two", None, _scorecard(0.1, 0.9, 0.5))
    b = Candidate("b", "line two\nline three", None, _scorecard(0.2, 0.95, 0.5))
    merged = merge(deterministic_merge_client(), a, b)
    assert "line one" in merged and "line two" in merged and "line three" in merged
    assert merged.count("line two") == 1  # deduplicated


# ---------------------------------------------------------------------------
# run_gepa end to end (fully offline, deterministic)
# ---------------------------------------------------------------------------


def test_run_gepa_finds_and_promotes_a_measurable_improvement():
    cases = [_case(f"c{i}", i) for i in range(6)]
    factory = prefix_sensitive_draft_fn_factory()

    result = run_gepa(
        base_prefix="Draft a reply on the user's behalf.",
        cases=cases,
        candidate_fn_factory=factory,
        judge_client=deterministic_judge_client(),
        reflection_client=deterministic_reflection_client(),
        rollout_budget=60,
        minibatch_size=3,
        seed=1,
    )

    assert result.baseline.scorecard.edit_burden_proxy is not None
    non_baseline = [c for c in result.frontier if c.label != "baseline"]
    assert non_baseline, "expected GEPA to find at least one surviving candidate"
    best = min(non_baseline, key=lambda c: c.scorecard.edit_burden_proxy)
    assert best.scorecard.edit_burden_proxy < result.baseline.scorecard.edit_burden_proxy
    assert MAGIC_MARKER in best.stable_prefix


def test_run_gepa_with_no_cases_returns_baseline_only():
    result = run_gepa(
        base_prefix="Draft a reply.", cases=[],
        candidate_fn_factory=prefix_sensitive_draft_fn_factory(),
        judge_client=deterministic_judge_client(), reflection_client=deterministic_reflection_client(),
    )
    assert result.frontier == (result.baseline,)
    assert result.rollouts_used == 0
