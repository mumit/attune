"""``attune eval run`` orchestration (build prompt 27, task 7): assembles the
full :class:`~.report.EvalReport` from injected collaborators. Offline by
default — every collaborator here (candidate drafting function, judge
client, triage classifier, injection probe) is injected, so the whole thing
runs against fakes with no network, the same discipline every other model
collaborator in this codebase already follows.
"""

from __future__ import annotations

import glob
import json
import os
import random
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from ..orchestrator.ledger import compute_edit_metrics
from .agreement import domain_gates, load_agreement
from .injection import InjectionCase, InjectionOutcome, run_injection_suite
from .judge import PairwiseResult, judge_pairwise
from .report import DomainPairwise, EvalReport
from .schema import EvalCase
from .triage_eval import TriageCase, TriageEvalReport, run_triage_eval


def load_cases(cases_dir: str) -> list[EvalCase]:
    cases = []
    if not os.path.isdir(cases_dir):
        return cases
    for path in sorted(glob.glob(os.path.join(cases_dir, "*.json"))):
        with open(path) as f:
            cases.append(EvalCase.from_json(json.load(f)))
    return cases


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_eval(
    *,
    cases: Sequence[EvalCase],
    candidate_fn: Callable[[EvalCase], str],
    judge_client: Any,
    agreement_path: str,
    triage_cases: Sequence[TriageCase] = (),
    triage_fn: Callable[[TriageCase], Any] | None = None,
    injection_cases: Sequence[InjectionCase] = (),
    injection_probe: Callable[[InjectionCase], InjectionOutcome] | None = None,
    seed: int = 0,
) -> EvalReport:
    rng = random.Random(seed)
    agreement = load_agreement(agreement_path)

    edit_distances: list[float] = []
    by_domain: dict[str, list[PairwiseResult]] = {}
    for case in cases:
        candidate = candidate_fn(case)
        if case.kind.value == "edit":
            edit_distances.append(
                compute_edit_metrics(candidate, case.gold_text).distance_normalized
            )
        result = judge_pairwise(
            judge_client,
            case_id=case.case_id,
            context=case.inputs.get("incoming_summary", ""),
            candidate_text=candidate,
            gold_text=case.gold_text,
            rng=rng,
        )
        by_domain.setdefault(case.domain, []).append(result)

    pairwise = []
    for domain, results in by_domain.items():
        wins = sum(1 for r in results if r.reported_winner == "candidate")
        losses = sum(1 for r in results if r.reported_winner == "gold")
        ties = sum(1 for r in results if r.reported_winner == "tie")
        unresolved = sum(1 for r in results if r.reported_winner is None)
        pairwise.append(DomainPairwise(
            domain=domain, total=len(results), wins=wins, losses=losses, ties=ties,
            unresolved=unresolved,
            disagreement_rate=(unresolved / len(results)) if results else 0.0,
            agreement_rate=agreement.get(domain),
            gates=domain_gates(agreement, domain),
        ))

    triage_report: TriageEvalReport | None = None
    if triage_fn is not None and triage_cases:
        triage_report = run_triage_eval(triage_cases, triage_fn)

    injection_report = None
    if injection_probe is not None and injection_cases:
        injection_report = run_injection_suite(injection_cases, injection_probe)

    return EvalReport(
        edit_burden_proxy=(sum(edit_distances) / len(edit_distances)) if edit_distances else None,
        pairwise=tuple(pairwise),
        triage=triage_report,
        injection=injection_report,
        generated_at=_now_iso(),
    )
