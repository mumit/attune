"""The coverage guardrail proxy (build prompt 36, task 5)."""

from __future__ import annotations

from datetime import datetime, timezone

from attune.evals.schema import CaseKind, EvalCase
from attune.optimize.coverage import compute_coverage_proxy, is_substantive


def _case(kind: CaseKind, case_id: str) -> EvalCase:
    return EvalCase(
        case_id=case_id, kind=kind, domain="mail", action="draft_reply",
        inputs={"incoming_summary": "can you help with this"},
        retrieved_context_ids=(), prompt_version=1,
        proposed_text="old", gold_text="Sure, here is what I found." if kind is CaseKind.EDIT else "(no reply — the human rejected the draft and sent nothing)",
        captured_at=datetime.now(timezone.utc),
    )


def test_is_substantive_rejects_short_or_empty():
    assert not is_substantive(None)
    assert not is_substantive("")
    assert not is_substantive(".")
    assert is_substantive("A real drafted reply.")


def test_coverage_proxy_none_when_no_edit_cases():
    cases = [_case(CaseKind.REJECT, "r1"), _case(CaseKind.REJECT, "r2")]
    assert compute_coverage_proxy(cases, lambda c: "anything") is None


def test_coverage_proxy_scoped_to_edit_cases_only():
    cases = [_case(CaseKind.EDIT, "e1"), _case(CaseKind.EDIT, "e2"), _case(CaseKind.REJECT, "r1")]

    def declining_candidate(case: EvalCase) -> str:
        return ""  # a candidate that always goes silent

    assert compute_coverage_proxy(cases, declining_candidate) == 0.0


def test_coverage_proxy_full_when_every_edit_case_gets_a_substantive_draft():
    cases = [_case(CaseKind.EDIT, "e1"), _case(CaseKind.EDIT, "e2")]
    assert compute_coverage_proxy(cases, lambda c: "a perfectly reasonable reply") == 1.0


def test_reject_case_declining_does_not_hurt_coverage():
    """Declining on a REJECT-kind case (the human already confirmed no
    reply was wanted) must never be counted against coverage -- only
    EDIT-kind silence is the RLUF failure mode."""
    cases = [_case(CaseKind.EDIT, "e1"), _case(CaseKind.REJECT, "r1")]

    def only_reply_to_edit(case: EvalCase) -> str:
        return "a real reply" if case.kind is CaseKind.EDIT else ""

    assert compute_coverage_proxy(cases, only_reply_to_edit) == 1.0
