"""EvalReport: JSON round-tripping (needed by the CI regression gate) and
the per-domain gates flag, exactly the acceptance criterion "a domain with
sub-75% agreement is reported but cannot fail the gate"."""

from __future__ import annotations

from attune.evals.injection import InjectionReport
from attune.evals.report import DomainPairwise, EvalReport, render_report_text
from attune.evals.triage_eval import TriageEvalReport


def test_domain_pairwise_win_rate_excludes_unresolved():
    p = DomainPairwise(
        domain="mail", total=10, wins=4, losses=2, ties=1, unresolved=3,
        disagreement_rate=0.3, agreement_rate=0.9, gates=True,
    )
    assert p.win_rate == 4 / 7


def test_domain_gates_flag_reflects_sub_threshold_agreement():
    below = DomainPairwise(
        domain="chat", total=5, wins=3, losses=1, ties=1, unresolved=0,
        disagreement_rate=0.0, agreement_rate=0.5, gates=False,
    )
    above = DomainPairwise(
        domain="mail", total=5, wins=3, losses=1, ties=1, unresolved=0,
        disagreement_rate=0.0, agreement_rate=0.9, gates=True,
    )
    assert below.gates is False
    assert above.gates is True


def test_eval_report_json_roundtrip():
    report = EvalReport(
        edit_burden_proxy=0.2,
        pairwise=(
            DomainPairwise("mail", 5, 3, 1, 1, 0, 0.0, 0.9, True),
            DomainPairwise("chat", 4, 2, 1, 1, 0, 0.0, 0.5, False),
        ),
        triage=TriageEvalReport(accuracy=0.8, confusion={("urgent", "urgent"): 4}, adjustment_correct_rate=0.75, total=5),
        injection=InjectionReport(success_rate=0.1, by_attack_type={"exfil_link": 0.2}, outcomes=()),
        generated_at="2026-01-01T00:00:00+00:00",
    )
    raw = report.to_json()
    restored = EvalReport.from_json(raw)

    assert restored.edit_burden_proxy == 0.2
    assert len(restored.pairwise) == 2
    assert restored.pairwise[0].domain == "mail"
    assert restored.pairwise[0].gates is True
    assert restored.pairwise[1].gates is False
    assert restored.triage.accuracy == 0.8
    assert restored.triage.confusion[("urgent", "urgent")] == 4
    assert restored.injection.success_rate == 0.1


def test_render_report_text_includes_all_sections():
    report = EvalReport(
        edit_burden_proxy=0.15,
        pairwise=(DomainPairwise("mail", 4, 2, 1, 1, 0, 0.0, 0.9, True),),
        triage=TriageEvalReport(accuracy=0.9, confusion={}, adjustment_correct_rate=0.8, total=10),
        injection=InjectionReport(success_rate=0.05, by_attack_type={"exfil_link": 0.1}, outcomes=()),
        generated_at="now",
    )
    text = render_report_text(report)
    assert "edit_burden_proxy" in text
    assert "mail" in text
    assert "triage accuracy" in text
    assert "injection success rate" in text


def test_render_report_text_handles_empty_report():
    report = EvalReport(edit_burden_proxy=None, pairwise=(), triage=None, injection=None, generated_at="now")
    text = render_report_text(report)
    assert "edit_burden_proxy: —" in text
