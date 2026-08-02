"""The weekly optimization job orchestrator (build prompt 36, task 1) --
the acceptance criterion: "one completed optimization run recorded end to
end: candidate versions, per-scorer deltas, the promotion decision, and the
resulting prompt version id"."""

from __future__ import annotations

from datetime import datetime, timezone

from attune import prompts
from attune.evals.offline_fakes import deterministic_judge_client
from attune.evals.schema import CaseKind, EvalCase
from attune.evals.triage_eval import TriageCase
from attune.optimize import offline_fakes as opt_fakes
from attune.optimize.job import run_weekly_optimization
from attune.orchestrator.triage import Priority, TriageResult


def _draft_case(i: int) -> EvalCase:
    return EvalCase(
        case_id=f"c{i}", kind=CaseKind.EDIT, domain="mail", action="draft_reply",
        inputs={"incoming_summary": f"Can you send the report by Friday? case {i}"},
        retrieved_context_ids=(), prompt_version=1, proposed_text="old draft",
        gold_text=f"Sure, I will send the report by Friday. (case {i})",
        captured_at=datetime.now(timezone.utc),
    )


_TRIAGE_CASES = [
    TriageCase(case_id="t1", incoming_summary="URGENT: deadline today", expected_priority=Priority.URGENT),
    TriageCase(case_id="t2", incoming_summary="please unsubscribe from this newsletter", expected_priority=Priority.NOISE),
    TriageCase(case_id="t3", incoming_summary="lets grab lunch sometime", expected_priority=Priority.ROUTINE),
]


def _always_routine_factory(prefix: str):
    def fn(case: TriageCase) -> TriageResult:
        return TriageResult(priority=Priority.ROUTINE, base_priority=Priority.ROUTINE, adjusted=False, reason="x")
    return fn


def test_full_run_records_candidate_versions_deltas_decision_and_new_version_id(tmp_path):
    versions_dir = str(tmp_path)
    draft_cases = [_draft_case(i) for i in range(6)]

    triage_good_factory = opt_fakes.prefix_sensitive_triage_fn_factory()

    def triage_factory(prefix: str):
        from attune.optimize.offline_fakes import TRIAGE_MAGIC_MARKER
        if TRIAGE_MAGIC_MARKER in prefix:
            return triage_good_factory(prefix)
        return _always_routine_factory(prefix)

    report = run_weekly_optimization(
        draft_cases=draft_cases,
        draft_candidate_fn_factory=opt_fakes.prefix_sensitive_draft_fn_factory(),
        judge_client=deterministic_judge_client(),
        reflection_client=opt_fakes.deterministic_reflection_client(),
        triage_cases=_TRIAGE_CASES,
        triage_fn_factory=triage_factory,
        instruction_client=opt_fakes.deterministic_instruction_proposer_client(),
        agreement_by_domain={"mail": 0.9},
        versions_dir=versions_dir,
        rollout_budget=60,
        minibatch_size=3,
        seed=1,
    )

    assert len(report.outcomes) == 2
    by_name = {o.name: o for o in report.outcomes}

    draft_outcome = by_name["draft"]
    assert draft_outcome.decision is not None
    assert draft_outcome.decision.approved
    assert draft_outcome.promoted_version is not None
    assert "edit_burden_proxy" in draft_outcome.scorer_deltas

    triage_outcome = by_name["triage"]
    assert triage_outcome.decision is not None
    assert triage_outcome.decision.approved
    assert triage_outcome.promoted_version is not None
    assert "triage_accuracy" in triage_outcome.scorer_deltas

    # The resulting prompt version id is real and traceable back to exact
    # text via the registry -- not just a number in the report.
    draft_records = prompts.history("draft", versions_dir=versions_dir)
    assert draft_records[-1].version == draft_outcome.promoted_version
    triage_records = prompts.history("triage", versions_dir=versions_dir)
    assert triage_records[-1].version == triage_outcome.promoted_version

    # Report round-trips through JSON (what the CLI/CI workflow actually emits).
    as_json = report.to_json()
    assert as_json["outcomes"][0]["promoted_version"] == draft_outcome.promoted_version


def test_domain_below_judge_agreement_threshold_is_excluded_from_the_reported_decision(tmp_path):
    versions_dir = str(tmp_path)
    draft_cases = [_draft_case(i) for i in range(6)]  # domain="mail"

    outcome = run_weekly_optimization(
        draft_cases=draft_cases,
        draft_candidate_fn_factory=opt_fakes.prefix_sensitive_draft_fn_factory(),
        judge_client=deterministic_judge_client(),
        reflection_client=opt_fakes.deterministic_reflection_client(),
        agreement_by_domain={"mail": 0.2},  # well below AGREEMENT_THRESHOLD
        versions_dir=versions_dir,
        rollout_budget=60,
        minibatch_size=3,
        seed=1,
    ).outcomes[0]

    assert outcome.decision is not None
    assert "mail" in outcome.decision.excluded_domains


def test_no_golden_cases_is_recorded_as_excluded_not_a_crash(tmp_path):
    report = run_weekly_optimization(
        draft_cases=[],
        draft_candidate_fn_factory=opt_fakes.prefix_sensitive_draft_fn_factory(),
        judge_client=deterministic_judge_client(),
        reflection_client=opt_fakes.deterministic_reflection_client(),
        versions_dir=str(tmp_path),
    )
    outcome = report.outcomes[0]
    assert outcome.decision is None
    assert outcome.promoted_version is None
    assert "excluded" in outcome.note


def test_missing_collaborators_omit_that_prompt_rather_than_failing_the_whole_run(tmp_path):
    """A job with no reflection client configured records nothing for
    ``draft`` but still completes -- an incompletely-configured job is not
    a crash."""
    report = run_weekly_optimization(
        triage_cases=_TRIAGE_CASES,
        triage_fn_factory=opt_fakes.prefix_sensitive_triage_fn_factory(),
        instruction_client=opt_fakes.deterministic_instruction_proposer_client(),
        versions_dir=str(tmp_path),
    )
    assert [o.name for o in report.outcomes] == ["triage"]
