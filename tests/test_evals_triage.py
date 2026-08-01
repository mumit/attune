"""The triage regression set (build prompt 27, task 5): accuracy, confusion,
and the learned-adjustment direction — the one number that proves "learns
what's important" rather than merely "classifies content well"."""

from __future__ import annotations

import os

from attune.orchestrator.importance import ImportanceTier, TierAssessment
from attune.orchestrator.triage import Priority, TriageResult, triage_thread
from attune.evals.triage_eval import TriageCase, load_triage_cases, run_triage_eval

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _ScriptedClassifyClient:
    """Maps each case's incoming_summary to a scripted PRIORITY/REASON
    response via a simple keyword rule the tests fully control."""

    def chat_completions_create(self, **kwargs):
        user = kwargs["messages"][-1]["content"].lower()
        if "urgent-marker" in user:
            priority = "URGENT"
        elif "noise-marker" in user:
            priority = "NOISE"
        else:
            priority = "ROUTINE"

        class _M:
            pass

        m = _M()
        m.content = f"PRIORITY: {priority}\nREASON: scripted"

        class _C:
            pass

        c = _C()
        c.message = m

        class _R:
            pass

        r = _R()
        r.choices = [c]
        return r


class _FixedTierProfile:
    def __init__(self, tier):
        self._tier = ImportanceTier(tier)

    def assess(self, sender, *, now=None):
        return TierAssessment(tier=self._tier, reason="fixed for test", pinned=False)


def _triage_fn(case: TriageCase) -> TriageResult:
    client = _ScriptedClassifyClient()
    profile = _FixedTierProfile(case.tier) if case.tier else None
    return triage_thread(client, case.incoming_summary, sender=case.sender, importance_profile=profile)


def test_run_triage_eval_accuracy_and_confusion():
    cases = [
        TriageCase("u1", "urgent-marker text", Priority.URGENT),
        TriageCase("r1", "plain text", Priority.ROUTINE),
        TriageCase("n1", "noise-marker text", Priority.URGENT),  # deliberately wrong
    ]
    report = run_triage_eval(cases, _triage_fn)
    assert report.total == 3
    assert report.accuracy == 2 / 3
    assert report.confusion[("urgent", "urgent")] == 1
    assert report.confusion[("routine", "routine")] == 1
    assert report.confusion[("urgent", "noise")] == 1


def test_adjustment_direction_demote_correct():
    case = TriageCase(
        "adj1", "urgent-marker text", Priority.URGENT,
        sender="s@example.com", tier="low", expected_adjustment="demote",
    )
    report = run_triage_eval([case], _triage_fn)
    assert report.adjustment_correct_rate == 1.0


def test_adjustment_direction_wrong_when_not_adjusted():
    # HIGH tier never touches URGENT (asymmetric rule) -- expecting "promote"
    # here is deliberately wrong, proving the metric catches a bad case.
    case = TriageCase(
        "adj2", "urgent-marker text", Priority.URGENT,
        sender="s@example.com", tier="high", expected_adjustment="promote",
    )
    report = run_triage_eval([case], _triage_fn)
    assert report.adjustment_correct_rate == 0.0


def test_adjustment_rate_is_none_without_any_adjustment_cases():
    cases = [TriageCase("r1", "plain text", Priority.ROUTINE)]
    report = run_triage_eval(cases, _triage_fn)
    assert report.adjustment_correct_rate is None


def test_load_the_checked_in_triage_case_set():
    cases = load_triage_cases(os.path.join(ROOT, "evals", "triage_cases.json"))
    assert len(cases) >= 30
    priorities = {c.expected_priority for c in cases}
    assert priorities == {Priority.URGENT, Priority.ROUTINE, Priority.NOISE}
    assert any(c.expected_adjustment == "demote" for c in cases)
    assert any(c.expected_adjustment == "promote" for c in cases)
    assert any(c.expected_adjustment == "none" for c in cases)
