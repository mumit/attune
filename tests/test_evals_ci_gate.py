"""The evals CI job's regression gate (build prompt 27, task 7): a pure
diff over two EvalReport snapshots. This is the test proving the ``evals``
job "demonstrably fails on a seeded regression"."""

from __future__ import annotations

import json

from attune.evals.ci_gate import DEFAULT_BUDGET, check_regression_budget, main
from attune.evals.injection import InjectionReport
from attune.evals.report import DomainPairwise, EvalReport
from attune.evals.triage_eval import TriageEvalReport


def _report(*, edit_burden=0.1, win_rate=0.8, gates=True, injection_rate=0.1, triage_accuracy=0.9):
    wins = round(win_rate * 10)
    return EvalReport(
        edit_burden_proxy=edit_burden,
        pairwise=(DomainPairwise("mail", 10, wins, 10 - wins, 0, 0, 0.0, 0.9 if gates else 0.5, gates),),
        triage=TriageEvalReport(accuracy=triage_accuracy, confusion={}, adjustment_correct_rate=0.8, total=10),
        injection=InjectionReport(success_rate=injection_rate, by_attack_type={}, outcomes=()),
        generated_at="now",
    )


def test_no_base_report_means_no_violations():
    current = _report()
    assert check_regression_budget(current, None) == []


def test_within_budget_is_clean():
    base = _report(edit_burden=0.10, win_rate=0.80, injection_rate=0.10, triage_accuracy=0.90)
    current = _report(edit_burden=0.11, win_rate=0.79, injection_rate=0.11, triage_accuracy=0.89)
    assert check_regression_budget(current, base) == []


def test_seeded_edit_burden_regression_fails():
    base = _report(edit_burden=0.10)
    current = _report(edit_burden=0.30)  # +0.20, budget is 0.05
    violations = check_regression_budget(current, base)
    assert any("edit_burden_proxy" in v for v in violations)


def test_seeded_pairwise_win_rate_regression_fails_only_when_gating():
    base = _report(win_rate=0.80, gates=True)
    current = _report(win_rate=0.30, gates=True)  # huge drop, gating domain
    violations = check_regression_budget(current, base)
    assert any("pairwise win rate" in v for v in violations)


def test_non_gating_domain_regression_never_fails():
    base = _report(win_rate=0.80, gates=False)
    current = _report(win_rate=0.10, gates=False)  # huge drop, but NOT gating
    assert check_regression_budget(current, base) == []


def test_seeded_injection_regression_fails():
    base = _report(injection_rate=0.05)
    current = _report(injection_rate=0.50)
    violations = check_regression_budget(current, base)
    assert any("injection success rate" in v for v in violations)


def test_seeded_triage_regression_fails():
    base = _report(triage_accuracy=0.90)
    current = _report(triage_accuracy=0.40)
    violations = check_regression_budget(current, base)
    assert any("triage accuracy" in v for v in violations)


def test_custom_budget_overrides_default():
    base = _report(edit_burden=0.10)
    current = _report(edit_burden=0.13)  # +0.03, within default 0.05 but not a tight budget
    assert check_regression_budget(current, base) == []
    tight = {**DEFAULT_BUDGET, "edit_burden_proxy": 0.01}
    violations = check_regression_budget(current, base, tight)
    assert violations


def test_main_cli_exits_nonzero_on_regression(tmp_path, capsys):
    base_path = tmp_path / "base.json"
    current_path = tmp_path / "current.json"
    base_path.write_text(json.dumps(_report(edit_burden=0.10).to_json()))
    current_path.write_text(json.dumps(_report(edit_burden=0.40).to_json()))

    rc = main([str(current_path), str(base_path)])
    assert rc == 1
    captured = capsys.readouterr()
    assert "REGRESSION" in captured.err


def test_main_cli_exits_zero_when_base_report_is_absent(tmp_path):
    current_path = tmp_path / "current.json"
    current_path.write_text(json.dumps(_report().to_json()))
    missing_base = tmp_path / "does_not_exist.json"

    rc = main([str(current_path), str(missing_base)])
    assert rc == 0
