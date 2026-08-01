"""``run_eval`` orchestration end to end (build prompt 27, task 7's
acceptance criteria): position-swapped judging is actually performed and
disagreement is reported; a sub-75%-agreement domain is reported but never
gates; edit-burden proxy is computed from real cases."""

from __future__ import annotations

from attune.evals.report import EvalReport
from attune.evals.runner import run_eval
from attune.evals.schema import CaseKind, EvalCase


def _case(case_id, domain, kind=CaseKind.EDIT, proposed="candidate text", gold="gold text"):
    return EvalCase(
        case_id=case_id, kind=kind, domain=domain, action="draft_reply",
        inputs={"incoming_summary": "context"}, retrieved_context_ids=(),
        prompt_version=None, proposed_text=proposed, gold_text=gold,
        captured_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )


class _AlwaysPrefersSlotA:
    def chat_completions_create(self, **kwargs):
        class _M:
            pass

        m = _M()
        m.content = "WINNER: A"

        class _C:
            pass

        c = _C()
        c.message = m

        class _R:
            pass

        r = _R()
        r.choices = [c]
        return r


class _PrefersCandidateByContent:
    """Judge that reliably (content-based, not slot-based) prefers whatever
    text equals 'candidate text' -- consistent under position swap."""

    def chat_completions_create(self, **kwargs):
        user = kwargs["messages"][-1]["content"]
        a_part = user.split("RESPONSE A:\n", 1)[1].split("\n\nRESPONSE B:\n")[0]
        winner = "A" if a_part.strip() == "candidate text" else "B"

        class _M:
            pass

        m = _M()
        m.content = f"WINNER: {winner}"

        class _C:
            pass

        c = _C()
        c.message = m

        class _R:
            pass

        r = _R()
        r.choices = [c]
        return r


def test_position_swap_disagreement_is_reported(tmp_path):
    cases = [_case("c1", "mail"), _case("c2", "mail")]
    report = run_eval(
        cases=cases,
        candidate_fn=lambda case: case.proposed_text,
        judge_client=_AlwaysPrefersSlotA(),
        agreement_path=str(tmp_path / "agreement.json"),
        seed=1,
    )
    mail = next(p for p in report.pairwise if p.domain == "mail")
    assert mail.unresolved == 2
    assert mail.disagreement_rate == 1.0
    assert mail.wins == 0 and mail.losses == 0 and mail.ties == 0


def test_consistent_judge_produces_a_clean_win_rate(tmp_path):
    cases = [_case("c1", "mail"), _case("c2", "mail")]
    report = run_eval(
        cases=cases,
        candidate_fn=lambda case: case.proposed_text,
        judge_client=_PrefersCandidateByContent(),
        agreement_path=str(tmp_path / "agreement.json"),
        seed=1,
    )
    mail = next(p for p in report.pairwise if p.domain == "mail")
    assert mail.unresolved == 0
    assert mail.wins == 2
    assert mail.win_rate == 1.0


def test_sub_threshold_agreement_domain_is_reported_but_never_gates(tmp_path):
    agreement_path = str(tmp_path / "agreement.json")
    with open(agreement_path, "w") as f:
        import json

        json.dump({"mail": 0.4, "chat": 0.9}, f)

    cases = [_case("c1", "mail"), _case("c2", "chat")]
    report = run_eval(
        cases=cases,
        candidate_fn=lambda case: case.proposed_text,
        judge_client=_PrefersCandidateByContent(),
        agreement_path=agreement_path,
        seed=1,
    )
    mail = next(p for p in report.pairwise if p.domain == "mail")
    chat = next(p for p in report.pairwise if p.domain == "chat")
    assert mail.agreement_rate == 0.4
    assert mail.gates is False  # below 75% -> reported, never a gate
    assert chat.agreement_rate == 0.9
    assert chat.gates is True


def test_edit_burden_proxy_uses_edit_metrics_between_candidate_and_gold(tmp_path):
    case = _case("c1", "mail", kind=CaseKind.EDIT, proposed="hello world", gold="hello world")
    report = run_eval(
        cases=[case],
        candidate_fn=lambda c: c.proposed_text,
        judge_client=_PrefersCandidateByContent(),
        agreement_path=str(tmp_path / "agreement.json"),
        seed=1,
    )
    assert report.edit_burden_proxy == 0.0  # verbatim match -> zero distance


def test_reject_case_excluded_from_edit_burden_proxy(tmp_path):
    case = _case("c1", "mail", kind=CaseKind.REJECT, proposed="draft", gold="(no reply)")
    report = run_eval(
        cases=[case],
        candidate_fn=lambda c: "totally different text",
        judge_client=_PrefersCandidateByContent(),
        agreement_path=str(tmp_path / "agreement.json"),
        seed=1,
    )
    assert report.edit_burden_proxy is None


def test_report_is_json_serializable_end_to_end(tmp_path):
    case = _case("c1", "mail")
    report = run_eval(
        cases=[case], candidate_fn=lambda c: c.proposed_text,
        judge_client=_PrefersCandidateByContent(),
        agreement_path=str(tmp_path / "agreement.json"), seed=1,
    )
    restored = EvalReport.from_json(report.to_json())
    assert restored.pairwise[0].domain == "mail"
