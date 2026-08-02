"""Candidate-prefix scoring shared by the reflective optimizer and the
promotion gate (build prompt 36, task 1)."""

from __future__ import annotations

from datetime import datetime, timezone

from attune.evals.offline_fakes import deterministic_judge_client
from attune.evals.schema import CaseKind, EvalCase
from attune.optimize.scoring import losing_trajectories, sample_trajectories, score_draft_candidate, score_trajectories


def _case(case_id: str, *, incoming: str, gold: str, kind: CaseKind = CaseKind.EDIT) -> EvalCase:
    return EvalCase(
        case_id=case_id, kind=kind, domain="mail", action="draft_reply",
        inputs={"incoming_summary": incoming}, retrieved_context_ids=(), prompt_version=1,
        proposed_text="old", gold_text=gold, captured_at=datetime.now(timezone.utc),
    )


def test_sample_trajectories_calls_candidate_fn_once_per_case():
    cases = [_case("a", incoming="hello there", gold="hi"), _case("b", incoming="thanks", gold="np")]
    calls = []

    def candidate_fn(case: EvalCase) -> str:
        calls.append(case.case_id)
        return "a reply"

    trajectories = sample_trajectories(cases, candidate_fn, deterministic_judge_client())
    assert calls == ["a", "b"]
    assert [t.case.case_id for t in trajectories] == ["a", "b"]


def test_losing_trajectories_only_keeps_gold_wins():
    cases = [_case("a", incoming="report status update please", gold="report status update please, done")]

    def bad_candidate(case: EvalCase) -> str:
        return "completely unrelated words"

    trajectories = sample_trajectories(cases, bad_candidate, deterministic_judge_client())
    losses = losing_trajectories(trajectories)
    # the deterministic judge fake picks whichever side shares more words
    # with the context -- the gold text shares far more here than the
    # unrelated candidate, so this should be a loss.
    assert len(losses) == 1


def test_score_trajectories_computes_edit_burden_only_over_edit_cases():
    cases = [
        _case("e1", incoming="x", gold="the exact right answer", kind=CaseKind.EDIT),
        _case("r1", incoming="y", gold="(no reply — the human rejected the draft and sent nothing)", kind=CaseKind.REJECT),
    ]

    def echo_gold(case: EvalCase) -> str:
        return case.gold_text

    scorecard = score_draft_candidate(cases, echo_gold, deterministic_judge_client())
    assert scorecard.edit_burden_proxy == 0.0  # candidate == gold exactly on the one EDIT case
    assert scorecard.coverage_proxy == 1.0


def test_score_trajectories_pairwise_grouped_by_domain():
    cases = [
        _case("m1", incoming="mail thing", gold="mail reply"),
    ]
    trajectories = sample_trajectories(cases, lambda c: "mail reply", deterministic_judge_client())
    scorecard = score_trajectories(trajectories)
    assert len(scorecard.pairwise) == 1
    assert scorecard.pairwise[0].domain == "mail"
