"""MIPROv2-style scalar bootstrapped instruction search over the ``triage``
prompt (build prompt 36, task 2) -- a scalar-only optimizer, since triage's
golden data is a label, not a human's own textual edit."""

from __future__ import annotations

from attune.evals.offline_fakes import FunctionClient
from attune.evals.triage_eval import TriageCase
from attune.optimize.mipro import _parse_candidates, run_mipro
from attune.optimize.offline_fakes import (
    TRIAGE_MAGIC_MARKER,
    deterministic_instruction_proposer_client,
    prefix_sensitive_triage_fn_factory,
)
from attune.orchestrator.triage import Priority, TriageResult

_CASES = [
    TriageCase(case_id="t1", incoming_summary="URGENT: deadline today", expected_priority=Priority.URGENT),
    TriageCase(case_id="t2", incoming_summary="please unsubscribe from this newsletter", expected_priority=Priority.NOISE),
    TriageCase(case_id="t3", incoming_summary="lets grab lunch sometime", expected_priority=Priority.ROUTINE),
]


def test_parse_candidates_splits_on_markers():
    text = "CANDIDATE 1:\nfirst prefix\nCANDIDATE 2:\nsecond prefix"
    assert _parse_candidates(text, n=2) == ["first prefix", "second prefix"]


def test_parse_candidates_handles_malformed_response():
    assert _parse_candidates("no markers here", n=4) == []


def test_run_mipro_finds_measurable_improvement_over_a_bad_baseline():
    def always_routine_factory(prefix: str):
        def fn(case: TriageCase) -> TriageResult:
            return TriageResult(priority=Priority.ROUTINE, base_priority=Priority.ROUTINE, adjusted=False, reason="x")
        return fn

    def instruction_responder(messages):
        return f"CANDIDATE 1:\n{TRIAGE_MAGIC_MARKER}"

    triage_factory = prefix_sensitive_triage_fn_factory()

    def factory(prefix: str):
        # baseline is the "always routine" bad classifier; any prefix
        # containing the marker becomes the prefix-sensitive good one.
        if TRIAGE_MAGIC_MARKER in prefix:
            return triage_factory(prefix)
        return always_routine_factory(prefix)

    result = run_mipro(
        base_prefix="classify the message",
        cases=_CASES,
        triage_fn_factory=factory,
        instruction_client=FunctionClient(instruction_responder),
    )
    assert result.best_score > result.baseline_score
    assert TRIAGE_MAGIC_MARKER in result.best_prefix
    assert result.report is not None
    assert result.report.accuracy == 1.0


def test_run_mipro_no_improvement_is_an_honest_outcome():
    factory = prefix_sensitive_triage_fn_factory()  # ignores prefix content unless the marker is present

    def instruction_responder(messages):
        return "CANDIDATE 1:\nsomething irrelevant that doesn't contain the marker"

    result = run_mipro(
        base_prefix="classify the message",
        cases=_CASES,
        triage_fn_factory=factory,
        instruction_client=FunctionClient(instruction_responder),
    )
    assert result.best_prefix == "classify the message"
    assert result.best_score == result.baseline_score


def test_run_mipro_with_no_cases_returns_zero_scores():
    result = run_mipro(
        base_prefix="p", cases=[], triage_fn_factory=prefix_sensitive_triage_fn_factory(),
        instruction_client=deterministic_instruction_proposer_client(),
    )
    assert result.best_prefix == "p"
    assert result.candidates_tried == 0
    assert result.report is None


def test_run_mipro_with_no_misclassified_cases_skips_the_instruction_call():
    """If the baseline is already perfect, MIPRO must not even ask for
    candidates -- there's nothing to bootstrap from."""
    perfect_cases = [
        TriageCase(case_id="p1", incoming_summary="urgent deadline", expected_priority=Priority.URGENT),
    ]

    def always_urgent_factory(prefix: str):
        def fn(case: TriageCase) -> TriageResult:
            return TriageResult(priority=Priority.URGENT, base_priority=Priority.URGENT, adjusted=False, reason="x")
        return fn

    called = {"n": 0}

    def instruction_responder(messages):
        called["n"] += 1
        return "CANDIDATE 1:\nshould never be reached"

    result = run_mipro(
        base_prefix="p", cases=perfect_cases, triage_fn_factory=always_urgent_factory,
        instruction_client=FunctionClient(instruction_responder),
    )
    assert called["n"] == 0
    assert result.candidates_tried == 0
