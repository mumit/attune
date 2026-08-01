"""The triage regression set (build prompt 27, task 5): hand-labelled
threads with an expected URGENT/ROUTINE/NOISE priority, plus — for cases
that carry one — the expected DIRECTION of ``triage._apply_importance_adjustment``
(promote/demote/none). Accuracy and confusion answer "is triage accurate";
the adjustment-direction rate is the one number that proves "learns what's
important" rather than merely "classifies content well" — a system could
score well on raw accuracy while its LOW/HIGH tier adjustment silently does
nothing, or moves the wrong way, and accuracy alone would never show it.

Cases live in ``evals/triage_cases.json`` (loaded by :func:`load_triage_cases`).
Running the set (:func:`run_triage_eval`) takes an injected ``triage_fn`` —
production wiring calls the REAL ``orchestrator.triage.triage_thread`` against
a scripted classify client and a fake/real ``ImportanceProfile``; this module
never reimplements triage's own rules, only scores its output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Sequence

from ..orchestrator.triage import Priority, TriageResult

_RANK = {Priority.NOISE: 0, Priority.ROUTINE: 1, Priority.URGENT: 2}


@dataclass(frozen=True)
class TriageCase:
    case_id: str
    incoming_summary: str
    expected_priority: Priority
    sender: str | None = None
    tier: str | None = None
    expected_adjustment: str | None = None  # "promote" | "demote" | "none"


def load_triage_cases(path: str) -> list[TriageCase]:
    with open(path) as f:
        raw = json.load(f)
    return [
        TriageCase(
            case_id=c["case_id"],
            incoming_summary=c["incoming_summary"],
            expected_priority=Priority(c["expected_priority"].lower()),
            sender=c.get("sender"),
            tier=c.get("tier"),
            expected_adjustment=c.get("expected_adjustment"),
        )
        for c in raw["cases"]
    ]


@dataclass(frozen=True)
class TriageEvalReport:
    accuracy: float
    confusion: dict[tuple[str, str], int]
    adjustment_correct_rate: float | None
    total: int


def _adjustment_matches(case: TriageCase, result: TriageResult) -> bool:
    if case.expected_adjustment == "none":
        return not result.adjusted
    if not result.adjusted:
        return False
    base = result.base_priority if result.base_priority is not None else result.priority
    if case.expected_adjustment == "promote":
        return _RANK[result.priority] > _RANK[base]
    if case.expected_adjustment == "demote":
        return _RANK[result.priority] < _RANK[base]
    return False


def run_triage_eval(
    cases: Sequence[TriageCase],
    triage_fn: Callable[[TriageCase], TriageResult],
) -> TriageEvalReport:
    confusion: dict[tuple[str, str], int] = {}
    correct = 0
    adjustment_total = 0
    adjustment_correct = 0

    for case in cases:
        result = triage_fn(case)
        key = (case.expected_priority.value, result.priority.value)
        confusion[key] = confusion.get(key, 0) + 1
        if result.priority == case.expected_priority:
            correct += 1
        if case.expected_adjustment is not None:
            adjustment_total += 1
            if _adjustment_matches(case, result):
                adjustment_correct += 1

    return TriageEvalReport(
        accuracy=(correct / len(cases)) if cases else 0.0,
        confusion=confusion,
        adjustment_correct_rate=(
            adjustment_correct / adjustment_total if adjustment_total else None
        ),
        total=len(cases),
    )
