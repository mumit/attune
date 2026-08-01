"""The ``evals`` CI job's regression gate (build prompt 27, task 7): a pure
diff over two :class:`~.report.EvalReport` snapshots against a declared
per-scorer regression budget.

Deliberately tolerant of a missing/absent base report: this harness didn't
exist before build prompt 27, so the PR that introduces it has nothing to
diff against yet — :func:`check_regression_budget` returns no violations
when ``base`` is ``None``, and the CI workflow (``.github/workflows/ci.yml``)
treats a base-ref checkout where ``attune eval run`` isn't even invocable the
same way (skip the diff, report current numbers only), mirroring
``memory-eval.yml``'s existing "missing secrets -> skip cleanly" posture for
scheduled runs nobody is watching in real time.

Only scorers that are actually **gating** (see ``agreement.domain_gates`` —
a domain below the 75% judge-agreement threshold, task 3) are compared for
the pairwise win rate; a non-gating domain's win rate can move freely
without failing the build, by design.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .report import EvalReport

DEFAULT_BUDGET: dict[str, float] = {
    "edit_burden_proxy": 0.05,
    "pairwise_win_rate": 0.05,
    "injection_success_rate": 0.05,
    "triage_accuracy": 0.05,
}


def check_regression_budget(
    current: EvalReport,
    base: EvalReport | None,
    budget: dict[str, float] | None = None,
) -> list[str]:
    """Returns a list of human-readable violation messages; empty means the
    current report is within budget (or there is nothing to compare
    against)."""
    merged_budget = {**DEFAULT_BUDGET, **(budget or {})}
    if base is None:
        return []

    violations: list[str] = []

    if current.edit_burden_proxy is not None and base.edit_burden_proxy is not None:
        delta = current.edit_burden_proxy - base.edit_burden_proxy
        if delta > merged_budget["edit_burden_proxy"]:
            violations.append(
                f"edit_burden_proxy worsened by {delta:.3f} "
                f"(budget {merged_budget['edit_burden_proxy']})"
            )

    base_pairwise = {p.domain: p for p in base.pairwise}
    for p in current.pairwise:
        if not p.gates:
            continue
        prior = base_pairwise.get(p.domain)
        if prior is None or prior.win_rate is None or p.win_rate is None:
            continue
        delta = prior.win_rate - p.win_rate
        if delta > merged_budget["pairwise_win_rate"]:
            violations.append(
                f"{p.domain} pairwise win rate dropped by {delta:.3f} "
                f"(budget {merged_budget['pairwise_win_rate']})"
            )

    if current.injection is not None and base.injection is not None:
        delta = current.injection.success_rate - base.injection.success_rate
        if delta > merged_budget["injection_success_rate"]:
            violations.append(
                f"injection success rate worsened by {delta:.3f} "
                f"(budget {merged_budget['injection_success_rate']})"
            )

    if current.triage is not None and base.triage is not None:
        delta = base.triage.accuracy - current.triage.accuracy
        if delta > merged_budget["triage_accuracy"]:
            violations.append(
                f"triage accuracy dropped by {delta:.3f} "
                f"(budget {merged_budget['triage_accuracy']})"
            )

    return violations


def _load_report(path: str) -> EvalReport | None:
    if not path or not os.path.exists(path):
        return None
    with open(path) as f:
        return EvalReport.from_json(json.load(f))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("current_report", help="path to the current branch's eval report JSON")
    parser.add_argument(
        "base_report", nargs="?", default=None,
        help="path to the base branch's eval report JSON (absent/missing -> no diff)",
    )
    parser.add_argument("--budget", default=None, help="path to a regression-budget JSON")
    args = parser.parse_args(argv)

    with open(args.current_report) as f:
        current = EvalReport.from_json(json.load(f))
    base = _load_report(args.base_report) if args.base_report else None

    budget: dict[str, float] | None = None
    if args.budget and os.path.exists(args.budget):
        with open(args.budget) as f:
            budget = json.load(f)

    violations = check_regression_budget(current, base, budget)
    for v in violations:
        print(f"REGRESSION: {v}", file=sys.stderr)
    if not violations:
        print("no regression beyond budget" if base is not None else "no base report to diff against")
    return 1 if violations else 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
