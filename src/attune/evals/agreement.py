"""Judge-human agreement (build prompt 27, task 3): ``attune eval label``
hand-labels a sample of (candidate, gold) pairs, :func:`compute_agreement`
turns that into the per-domain rate, and :func:`domain_gates` is the
enforcement — **in code, not in a comment** — that a domain whose agreement
falls below :data:`AGREEMENT_THRESHOLD` cannot fail the CI gate on its
pairwise result.

An unmeasured domain (no agreement record at all) is treated the same as a
known-bad one: :func:`domain_gates` returns ``False`` for it. "We haven't
proven this judge is trustworthy yet" and "we've proven it isn't" both mean
the same thing to a release gate — don't trust it either way.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Sequence

AGREEMENT_THRESHOLD = 0.75


@dataclass(frozen=True)
class LabelRecord:
    case_id: str
    domain: str
    human_choice: str  # "candidate" | "gold" | "tie"
    judge_choice: str  # "candidate" | "gold" | "tie"

    def agrees(self) -> bool:
        return self.human_choice == self.judge_choice

    def to_json(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "domain": self.domain,
            "human_choice": self.human_choice,
            "judge_choice": self.judge_choice,
        }


def compute_agreement(records: Sequence[LabelRecord]) -> dict[str, float]:
    """Per-domain judge-human agreement rate over a labeled sample."""
    by_domain: dict[str, list[LabelRecord]] = {}
    for r in records:
        by_domain.setdefault(r.domain, []).append(r)
    return {
        domain: sum(1 for r in recs if r.agrees()) / len(recs)
        for domain, recs in by_domain.items()
    }


def domain_gates(
    agreement_by_domain: dict[str, float], domain: str, *, threshold: float = AGREEMENT_THRESHOLD
) -> bool:
    """Whether ``domain``'s pairwise result is trusted enough to fail CI."""
    rate = agreement_by_domain.get(domain)
    return rate is not None and rate >= threshold


def load_agreement(path: str) -> dict[str, float]:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def save_agreement(path: str, agreement: dict[str, float]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as f:
        json.dump(agreement, f, indent=2, sort_keys=True)
        f.write("\n")


def run_label_session(
    cases: Sequence[Any],
    *,
    judge_fn: Callable[[Any], str],
    ask_human: Callable[[Any], str],
    labels_path: str,
) -> list[LabelRecord]:
    """Hand-label a sample of stored cases (target 150-200 pairs — see the
    build prompt; this runs over however many ``cases`` the caller passes,
    since a golden set that "grows from real decisions" won't reach that
    target on day one). ``judge_fn``/``ask_human`` each take one ``EvalCase``
    and return one of ``"candidate"``/``"gold"``/``"tie"`` — judging the
    SAME stored ``proposed_text`` vs ``gold_text`` pair the human sees, so
    agreement measures the judge against the human on an identical
    comparison, not a freshly regenerated one.

    Labels are appended (not overwritten) to ``labels_path`` — a JSONL file
    per domain sample, so repeated labeling sessions accumulate toward the
    target sample size rather than each session losing the last one's
    work."""
    records = []
    for case in cases:
        judge_choice = judge_fn(case)
        human_choice = ask_human(case)
        records.append(LabelRecord(case.case_id, case.domain, human_choice, judge_choice))

    parent = os.path.dirname(labels_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(labels_path, "a") as f:
        for r in records:
            f.write(json.dumps(r.to_json(), sort_keys=True) + "\n")
    return records


def load_labels(path: str) -> list[LabelRecord]:
    if not os.path.exists(path):
        return []
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            records.append(
                LabelRecord(raw["case_id"], raw["domain"], raw["human_choice"], raw["judge_choice"])
            )
    return records
